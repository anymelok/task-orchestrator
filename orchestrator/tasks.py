import contextlib
import contextvars
import datetime
import logging

import redis
from celery.exceptions import SoftTimeLimitExceeded
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from config.celery import app

from .agent import (
    AgentConnectionError,
    AgentExecutionError,
    AgentTimeoutError,
    get_agent_client,
)
from .models import (
    Approval,
    ApprovalStatus,
    CommandType,
    Execution,
    ExecutionStatus,
    Host,
    HostBlock,
    Job,
    JobStatus,
)

job_id_var = contextvars.ContextVar('job_id', default='N/A')


@contextlib.contextmanager
def job_context(job_id):
    """
    Контекстный менеджер для автоматической привязки job_id к логам
    """
    token = job_id_var.set(str(job_id))
    try:
        yield
    finally:
        job_id_var.reset(token)


class JobIdFilter(logging.Filter):
    """
    Глобальный фильтр логирования
    """

    def filter(self, record):
        record.job_id = job_id_var.get()
        return True


logger = logging.getLogger(__name__)

redis_client = redis.from_url(settings.CELERY_BROKER_URL)


def requires_approval(command_type: str) -> bool:
    """Определяет, какие типы команд требуют ручного подтверждения"""
    return command_type == CommandType.DEPLOY


@app.task
def dispatch_job_task(job_pk: int):
    """
    Разбирает Job, находит целевые хосты и планирует запуски
    """
    with job_context(job_pk):
        try:
            job = Job.objects.get(pk=job_pk)
        except Job.DoesNotExist:
            logger.error(f'Job with pk {job_pk} not found.')
            return

        target_hosts = job.payload.get('target_hosts')
        target_selector = job.payload.get('target_selector')

        hosts = []
        if target_hosts:
            for hostname in target_hosts:
                host, _ = Host.objects.get_or_create(hostname=hostname)
                hosts.append(host)
        elif target_selector:
            filters = {f'metadata__{k}': v for k, v in target_selector.items()}
            hosts = list(Host.objects.filter(**filters))

        if not hosts:
            job.status = JobStatus.FAILED
            job.save()
            logger.warning(f'No hosts matched for job {job_pk}.')
            return

        has_approved = Approval.objects.filter(job=job, status=ApprovalStatus.APPROVED).exists()

        # Проверка необходимости одобрения
        if requires_approval(job.command_type) and not has_approved:
            with transaction.atomic():
                job.status = JobStatus.WAIT_APPROVAL
                job.save()
                Approval.objects.get_or_create(job=job, status=ApprovalStatus.PENDING)
            logger.info(f'Job {job_pk} requires approval. Suspended.')
            return

        # Переводим Job в статус RUNNING сразу
        job.status = JobStatus.RUNNING
        job.save()

        executions_to_create = [
            Execution(
                job=job,
                host=host,
                status=ExecutionStatus.QUEUED,
                timeout_seconds=120,
            )
            for host in hosts
        ]

        Execution.objects.bulk_create(executions_to_create)

        created_executions = list(Execution.objects.filter(job=job))

        # Инициализируем атомарный счетчик оставшихся задач в Redis
        redis_client.set(f'job:remaining:{job.pk}', len(created_executions))

        # отправляем задачи на выполнение
        for execution in created_executions:
            run_execution_on_host_task.apply_async(
                args=[execution.pk],
                # жёсткие лимиты по времени, чтобы таски не висели
                time_limit=execution.timeout_seconds + 10,
                soft_time_limit=execution.timeout_seconds,
            )

        logger.info(f'Dispatched {len(created_executions)} executions for job {job_pk}')


@app.task(bind=True, max_retries=3)
def run_execution_on_host_task(self, execution_pk: int):
    """
    Исполняет команду на конкретном хосте с контролем конкурентности и очередями
    """
    try:
        execution = Execution.objects.select_related('job', 'host').get(pk=execution_pk)
    except Execution.DoesNotExist:
        logger.error(f'Execution with pk {execution_pk} not found')
        return

    with job_context(execution.job.pk):
        if execution.job.status == JobStatus.CANCELLED:
            _finalize_execution(execution, ExecutionStatus.CANCELLED, '[SYSTEM] Job was cancelled')
            return

        host = execution.host
        job = execution.job

        # проверка блокировки
        is_blocked_on_host = HostBlock.objects.filter(host=host, command_type=job.command_type).exists()

        if is_blocked_on_host:
            _finalize_execution(
                execution,
                ExecutionStatus.BLOCKED,
                f"[BLOCKED] Command '{job.command_type}' is explicitly prohibited on host {host.hostname}.",
            )
            logger.warning(
                f"Execution {execution_pk} BLOCKED: command '{job.command_type}' is prohibited on {host.hostname}"
            )
            return

        # Настройки блокировки
        lock_key = f'lock:host:{host.pk}'
        lock_timeout = execution.timeout_seconds + 30

        # Попытка захватить лок хоста в Redis
        is_locked = redis_client.set(lock_key, f'exec_{execution_pk}', ex=lock_timeout, nx=True)

        if not is_locked:
            # Если хост занят, добавляем execution_pk в очередь хоста в Redis и выходим
            queue_key = f'host:queue:{host.pk}'
            redis_client.rpush(queue_key, execution_pk)
            logger.info(f'Host {host.hostname} is busy. Queued execution {execution_pk} in Redis queue')
            return

        logger.info(f'Lock acquired for host {host.hostname}. Starting execution {execution_pk}')

        execution.status = ExecutionStatus.RUNNING
        execution.last_attempt_at = timezone.now()
        execution.save()

        agent_client = get_agent_client()

        try:
            # Вызов агента
            logs = agent_client.execute(
                hostname=host.hostname,
                command_type=job.command_type,
                payload=job.payload.get('params', {}),
                timeout=execution.timeout_seconds,
            )
            _finalize_execution(execution, ExecutionStatus.SUCCESS, logs)

        except SoftTimeLimitExceeded as exc:
            # обработка таймаута на уровне воркера Celery
            _finalize_execution(
                execution,
                ExecutionStatus.TIMEOUT,
                f'[TIMEOUT ERROR] Celery soft time limit exceeded.\nDetails: {str(exc)}',
            )
            logger.error(f'Execution {execution_pk} exceeded soft time limit')

        except AgentConnectionError as exc:
            # сетевая ошибка: планируем повтор, но сохраняем текущий лок хоста для таски
            execution.retry_count += 1
            execution.save()

            if self.request.retries < self.max_retries:
                backoff = 5 * (2**self.request.retries)
                logger.warning(
                    f'Network failure on {host.hostname}. Retrying execution {execution_pk} '
                    f'in {backoff}s (Attempt {self.request.retries + 1}/{self.max_retries})'
                )
                # Перед повторным планированием освобождаем лок,
                # чтобы пропустить другие задачи, если они есть
                redis_client.delete(lock_key)
                raise self.retry(exc=exc, countdown=backoff)
            else:
                _finalize_execution(
                    execution,
                    ExecutionStatus.FAILED,
                    f'[CONNECTION ERROR] Failed after {self.max_retries} retries.\nDetails: {str(exc)}',
                )

        except AgentTimeoutError as exc:
            _finalize_execution(
                execution,
                ExecutionStatus.TIMEOUT,
                f'[TIMEOUT ERROR] Execution timed out.\nDetails: {str(exc)}',
            )

        except AgentExecutionError as exc:
            _finalize_execution(
                execution,
                ExecutionStatus.FAILED,
                f'[EXECUTION ERROR] Script failed.\n{exc.logs}',
            )

        except Exception as exc:
            _finalize_execution(
                execution,
                ExecutionStatus.FAILED,
                f'[CRITICAL ERROR] Unexpected orchestrator exception:\n{str(exc)}',
            )
            logger.error(
                f'Critical exception during execution {execution_pk}: {str(exc)}',
                exc_info=True,
            )

        finally:
            # Освобождаем лок текущей задачи
            redis_client.delete(lock_key)
            logger.info(f'Lock released for host {host.hostname} (execution {execution_pk})')

            # 2. Очереди на уровне Redis:
            # Проверяем, есть ли другие задачи в очереди ожидания для этого хоста
            next_exec_pk = redis_client.lpop(f'host:queue:{host.pk}')
            if next_exec_pk:
                pk_str = next_exec_pk.decode('utf-8') if isinstance(next_exec_pk, bytes) else str(next_exec_pk)
                # Запускаем следующую задачу из очереди хоста
                logger.info(f'Triggering next queued execution {pk_str} for host {host.hostname}')
                run_execution_on_host_task.delay(int(pk_str))


def _finalize_execution(execution: Execution, status: str, logs: str):
    """
    Вспомогательный метод для завершения задачи.
    Устанавливает статус и атомарно уменьшает счетчик оставшихся хостов
    """
    execution.status = status
    execution.logs = logs
    execution.save(update_fields=['status', 'logs', 'updated_at'])

    # уменьшаем счетчик оставшихся хостов для задачи
    job_pk = execution.job.pk
    remaining = redis_client.decr(f'job:remaining:{job_pk}')

    if remaining == 0:
        # только последний завершившийся хост запускает обновление глобального статуса Job
        _update_job_aggregated_status(execution.job)
        redis_client.delete(f'job:remaining:{job_pk}')


def _update_job_aggregated_status(job: Job):
    """
    Финальное обновление статуса Job.
    Вызывается 1 раз за весь жизненный цикл задачи
    """
    if job.status == JobStatus.CANCELLED:
        return

    # Проверяем, были ли какие-то ошибки во время выполнения
    has_failures = job.executions.filter(
        status__in=[
            ExecutionStatus.FAILED,
            ExecutionStatus.TIMEOUT,
            ExecutionStatus.BLOCKED,
            ExecutionStatus.CANCELLED,
        ]
    ).exists()

    new_status = JobStatus.FAILED if has_failures else JobStatus.SUCCESS

    job.status = new_status
    job.save(update_fields=['status', 'updated_at'])
    logger.info(f'Job {job.pk} finalized with global status: {new_status}')


# фоновый таск санитара БД
@app.task
def cleanup_orphaned_executions_task():
    """
    Фоновый Санитар: находит зависшие в RUNNING задачи (e.g. упал воркер)
    и безопасно переводит их в TIMEOUT, освобождая блокировки хостов
    """
    # Таска считается зависшей, если работает больше 15 минут
    threshold = timezone.now() - datetime.timedelta(minutes=15)
    orphaned_executions = Execution.objects.filter(status=ExecutionStatus.RUNNING, updated_at__lt=threshold)

    count = orphaned_executions.count()
    if count == 0:
        return

    for execution in orphaned_executions:
        # освобождаем лок хоста
        redis_client.delete(f'lock:host:{execution.host.pk}')
        # завершаем выполнение с TIMEOUT
        _finalize_execution(
            execution,
            ExecutionStatus.TIMEOUT,
            '[SYSTEM] Marked as TIMEOUT by background cleanup (worker inactivity)',
        )

    logger.info(f'Successfully cleaned up {count} orphaned executions')
