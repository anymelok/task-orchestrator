import json

from django.core.paginator import EmptyPage, Paginator
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import (
    Approval,
    ApprovalStatus,
    AuditLog,
    CommandType,
    Execution,
    ExecutionStatus,
    Host,
    HostBlock,
    Job,
    JobStatus,
)


# запуск фоновой celery-задачи планирования
def trigger_dispatch_job(job_pk: int):
    from .tasks import dispatch_job_task

    # линтер здесь ругается на .delay(),
    # т.к. не видит динамически добавляемые декоратором методы
    dispatch_job_task.delay(job_pk)  # type: ignore


@csrf_exempt
@require_POST
def webhook_jobs(request):
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    # валидация обязательных полей
    external_id = data.get("external_id")
    command_type = data.get("command_type")
    payload_params = data.get("payload", {})
    hosts_list = data.get("hosts")
    selector = data.get("selector")

    if not external_id:
        return JsonResponse({"error": "Missing 'external_id'"}, status=400)
    if not command_type or command_type not in CommandType.values:
        return JsonResponse(
            {"error": f"Invalid or missing 'command_type'. Available: {CommandType.values}"},
            status=400,
        )
    if not hosts_list and not selector:
        return JsonResponse({"error": "Either 'hosts' or 'selector' must be provided"}, status=400)

    """
    Идемпотентность и атомарное создание Job
    
    Используем уникальность external_id на уровне БД 
    В случае race condition второй параллельный запрос 
    вызовет IntegrityError, который мы перехватим
    """

    try:
        with transaction.atomic():
            job = Job.objects.create(
                external_id=external_id,
                command_type=command_type,
                payload={
                    "params": payload_params,
                    "target_hosts": hosts_list,
                    "target_selector": selector,  # селектор хостов
                },
                status=JobStatus.NEW,
            )

            AuditLog.objects.create(
                action="job_created",
                details={
                    "job_id": job.pk,
                    "external_id": external_id,
                    "command_type": command_type,
                },
            )
            created = True
    except IntegrityError:
        # если такой external_id уже существует,
        # находим его и возвращаем существующий pk
        job = Job.objects.get(external_id=external_id)
        created = False

    if created:
        # ставим в очередь Celery задачу на планирование
        trigger_dispatch_job(job.pk)
        return JsonResponse({"job_id": job.pk, "status": job.status}, status=201)

    else:
        return JsonResponse(
            {
                "job_id": job.pk,
                "status": job.status,
                "message": "Job with this external_id already exists!",
            },
            status=200,
        )


# APPROVAL API
@csrf_exempt
@require_POST
def job_approve(request, job_id):
    """
    Эндпоинт ручного одобрения задачи
    URL: POST /jobs/{job_id}/approve
    """
    try:
        # Находим запись ожидания аппрува
        approval = Approval.objects.select_related("job").get(job_id=job_id)
    except Approval.DoesNotExist:
        return JsonResponse({"error": f"Pending approval for job {job_id} not found"}, status=404)

    if approval.status != ApprovalStatus.PENDING:
        return JsonResponse({"error": f"Approval already in state: {approval.status}"}, status=400)

    with transaction.atomic():
        approval.status = ApprovalStatus.APPROVED
        approval.resolved_by = "admin"
        approval.resolved_at = timezone.now()
        approval.save()

        AuditLog.objects.create(action="job_approved", details={"job_id": job_id, "by": "admin"})

    # Запускаем таску планирования заново.
    # она пропустит шаг блокировки и создаст Executions
    trigger_dispatch_job(job_id)

    return JsonResponse({"message": f"Job {job_id} successfully approved", "status": "QUEUED"})


@csrf_exempt
@require_POST
def job_reject(request, job_id):
    """
    Эндпоинт отклонения задачи
    URL: POST /jobs/{job_id}/reject
    """
    try:
        approval = Approval.objects.select_related("job").get(job_id=job_id)
    except Approval.DoesNotExist:
        return JsonResponse({"error": f"Pending approval for job {job_id} not found"}, status=404)

    if approval.status != ApprovalStatus.PENDING:
        return JsonResponse({"error": f"Approval already in state: {approval.status}"}, status=400)

    with transaction.atomic():
        approval.status = ApprovalStatus.REJECTED
        approval.resolved_by = "admin"
        approval.resolved_at = timezone.now()
        approval.save()

        # Помечаем сам Job как отмененный
        job = approval.job
        job.status = JobStatus.CANCELLED
        job.save()

        AuditLog.objects.create(action="job_rejected", details={"job_id": job_id, "by": "admin"})

    return JsonResponse({"message": f"Job {job_id} was rejected", "status": "CANCELLED"})


# HOST LOCKS API
@csrf_exempt
def host_blocks(request, host_id):
    """
    Установка списка блокировок на хосте
    URL: PUT /hosts/{host_id}/blocks
    """
    if request.method != "PUT":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    try:
        host = Host.objects.get(pk=host_id)
    except Host.DoesNotExist:
        return JsonResponse({"error": f"Host with id {host_id} not found"}, status=404)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    command_types = data.get("command_types", [])
    for ct in command_types:
        if ct not in CommandType.values:
            return JsonResponse(
                {"error": f"Invalid command type '{ct}'. Available: {CommandType.values}"},
                status=400,
            )

    # Атомарно обновляем черный список команд на хосте
    with transaction.atomic():
        # очищаем старые блокировки
        HostBlock.objects.filter(host=host).delete()
        # Создаем новые
        blocks_to_create = [HostBlock(host=host, command_type=ct) for ct in command_types]
        HostBlock.objects.bulk_create(blocks_to_create)

        AuditLog.objects.create(
            action="host_blocks_updated",
            details={
                "host_id": host.pk,
                "hostname": host.hostname,
                "blocked_commands": command_types,
            },
        )

    return JsonResponse(
        {
            "message": f"Successfully updated blocks for host {host.hostname}",
            "blocks": command_types,
        }
    )


@csrf_exempt
def host_block_delete(request, host_id, command_type):
    """
    Удаление блокировки конкретной команды на хосте
    URL: DELETE /hosts/{host_id}/blocks/{command_type}
    """
    if request.method != "DELETE":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    if command_type not in CommandType.values:
        return JsonResponse(
            {"error": f"Invalid command type. Available: {CommandType.values}"},
            status=400,
        )

    deleted_count, _ = HostBlock.objects.filter(host_id=host_id, command_type=command_type).delete()

    if deleted_count == 0:
        return JsonResponse(
            {"error": f"Block for {command_type} on host {host_id} not found"},
            status=404,
        )

    return JsonResponse({"message": f"Successfully removed block for {command_type} on host {host_id}"})


# MONITORING API
def job_detail(request, job_id):
    """
    Агрегированный статус задачи
    URL: GET /jobs/{job_id}
    """
    try:
        job = Job.objects.get(pk=job_id)
    except Job.DoesNotExist:
        return JsonResponse({"error": f"Job with id {job_id} not found"}, status=404)

    stats = job.executions.aggregate(
        total=Count("id"),
        success=Count("id", filter=Q(status=ExecutionStatus.SUCCESS)),
        failed=Count("id", filter=Q(status=ExecutionStatus.FAILED)),
        running=Count("id", filter=Q(status=ExecutionStatus.RUNNING)),
        queued=Count("id", filter=Q(status=ExecutionStatus.QUEUED)),
        blocked=Count("id", filter=Q(status=ExecutionStatus.BLOCKED)),
        timeout=Count("id", filter=Q(status=ExecutionStatus.TIMEOUT)),
        cancelled=Count("id", filter=Q(status=ExecutionStatus.CANCELLED)),
    )

    return JsonResponse(
        {
            "job_id": job.pk,
            "command_type": job.command_type,
            "status": job.status,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "stats": stats,
        }
    )


def job_executions(request, job_id):
    """
    Детализация выполнения по хостам с фильтрацией и пагинацией
    URL: GET /jobs/{job_id}/executions?status=...&page=...&sort_by=...
    """
    try:
        job = Job.objects.get(pk=job_id)
    except Job.DoesNotExist:
        return JsonResponse({"error": f"Job with id {job_id} not found"}, status=404)

    # не грузим тяжелое текстовое поле logs (defer)
    executions = job.executions.select_related("host").defer("logs")

    # фильтрация по статусу
    status_filter = request.GET.get("status")
    if status_filter:
        if status_filter not in ExecutionStatus.values:
            return JsonResponse({"error": f"Invalid status filter '{status_filter}'"}, status=400)
        executions = executions.filter(status=status_filter)

    # безопасная сортировка только разрешенные поля
    sort_by = request.GET.get("sort_by", "-updated_at")
    allowed_sorts = [
        "updated_at",
        "-updated_at",
        "created_at",
        "-created_at",
        "status",
        "-status",
    ]
    if sort_by not in allowed_sorts:
        sort_by = "-updated_at"

    executions = executions.order_by(sort_by)

    # пагинация
    page_size = 50
    paginator = Paginator(executions, page_size)
    page_number = request.GET.get("page", 1)

    try:
        page_obj = paginator.get_page(page_number)
    except EmptyPage:
        return JsonResponse({"error": "Page not found"}, status=404)

    results = []
    for exe in page_obj:
        results.append(
            {
                "execution_id": exe.pk,
                "host_id": exe.host.pk,
                "hostname": exe.host.hostname,
                "status": exe.status,
                "retry_count": exe.retry_count,
                "last_attempt_at": exe.last_attempt_at,
                "updated_at": exe.updated_at,
            }
        )

    return JsonResponse(
        {
            "page": page_obj.number,
            "total_pages": paginator.num_pages,
            "total_items": paginator.count,
            "results": results,
        }
    )


def execution_logs(request, execution_id):
    """
    Получение логов и подробной информации по конкретному запуску
    URL: GET /executions/{execution_id}/logs
    """
    try:
        # здесь logs запрашивается явно, так как это детальный просмотр одного элемента
        execution = Execution.objects.select_related("host").get(pk=execution_id)
    except Execution.DoesNotExist:
        return JsonResponse({"error": f"Execution with id {execution_id} not found"}, status=404)

    return JsonResponse(
        {
            "execution_id": execution.pk,
            "hostname": execution.host.hostname,
            "status": execution.status,
            "logs": execution.logs,
            "retry_count": execution.retry_count,
            "last_attempt_at": execution.last_attempt_at,
            "updated_at": execution.updated_at,
        }
    )


@csrf_exempt
@require_POST
def job_cancel(request, job_id):
    """
    Отмена выполнения задачи
    URL: POST /jobs/{job_id}/cancel
    """
    try:
        job = Job.objects.get(pk=job_id)
    except Job.DoesNotExist:
        return JsonResponse({"error": f"Job with id {job_id} not found"}, status=404)

    # нельзя отменить уже завершенные задачи
    if job.status in [JobStatus.SUCCESS, JobStatus.FAILED, JobStatus.CANCELLED]:
        return JsonResponse({"error": f"Job is already in final state: {job.status}"}, status=400)

    with transaction.atomic():
        job.status = JobStatus.CANCELLED
        job.save(update_fields=["status", "updated_at"])

        # Массово отменяем в БД все executions, которые QUEUED или NEW
        # Если воркер возьмет их из очереди Celery,
        # он увидит статус CANCELLED и сразу выйдет
        job.executions.filter(status__in=[ExecutionStatus.NEW, ExecutionStatus.QUEUED]).update(
            status=ExecutionStatus.CANCELLED, logs="[SYSTEM] Cancelled by user request."
        )

        # логируем аудит отмены
        AuditLog.objects.create(action="job_cancelled", details={"job_id": job.pk, "by": "admin"})

    return JsonResponse({"message": f"Job {job_id} successfully cancelled", "status": "CANCELLED"})
