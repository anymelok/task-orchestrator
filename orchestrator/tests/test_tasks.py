import datetime

import pytest
from celery.exceptions import SoftTimeLimitExceeded
from django.utils import timezone

from orchestrator.models import (
    CommandType,
    Execution,
    ExecutionStatus,
    Host,
    Job,
    JobStatus,
)
from orchestrator.tasks import (
    cleanup_orphaned_executions_task,
    dispatch_job_task,
    run_execution_on_host_task,
)


@pytest.fixture(autouse=True)
def enable_eager_celery(settings):
    settings.CELERY_TASK_ALWAYS_EAGER = True
    settings.CELERY_TASK_EAGER_PROPAGATES = True


@pytest.mark.django_db
class TestCeleryTasksAndFailureHandling:
    def test_dispatch_job_no_matched_hosts(self):
        # Тестируем случай, когда ни один хост не подошел под селектор тегов
        job = Job.objects.create(
            external_id="dispatch-failed-1",
            command_type=CommandType.PING,
            payload={"target_selector": {"role": "non-existent-role-xyz"}},
        )
        dispatch_job_task(job.pk)

        job.refresh_from_db()
        assert job.status == JobStatus.FAILED

    def test_run_execution_soft_time_limit(self, mocker):
        # Эмулируем ошибку SoftTimeLimitExceeded в MockAgentClient
        mocker.patch(
            "orchestrator.agent.MockAgentClient.execute",
            side_effect=SoftTimeLimitExceeded(),
        )

        host, _ = Host.objects.get_or_create(hostname="host-timeout-test")
        job = Job.objects.create(
            external_id="timeout-id-1",
            command_type=CommandType.PING,
            payload={"target_hosts": ["host-timeout-test"]},
        )

        # Создаем execution вручную
        execution = Execution.objects.create(job=job, host=host, status=ExecutionStatus.QUEUED, timeout_seconds=5)

        run_execution_on_host_task(execution.pk)

        execution.refresh_from_db()
        assert execution.status == ExecutionStatus.TIMEOUT
        assert "soft time limit exceeded" in execution.logs

    def test_scavenger_task_cleanup(self):
        host, _ = Host.objects.get_or_create(hostname="host-scavenger")
        job = Job.objects.create(
            external_id="scavenger-id-1",
            command_type=CommandType.PING,
            status=JobStatus.RUNNING,
        )

        # Создаем "зависшую" в RUNNING таску, обновленную более 15 минут назад
        execution = Execution.objects.create(job=job, host=host, status=ExecutionStatus.RUNNING, timeout_seconds=120)
        # Искусственно откатываем время изменения назад
        Execution.objects.filter(pk=execution.pk).update(updated_at=timezone.now() - datetime.timedelta(minutes=20))

        cleanup_orphaned_executions_task()

        execution.refresh_from_db()
        assert execution.status == ExecutionStatus.TIMEOUT
        assert "Marked as TIMEOUT by background cleanup" in execution.logs
