import pytest
from django.urls import reverse

from orchestrator.models import AuditLog, CommandType, Host, Job, JobStatus


@pytest.fixture(autouse=True)
def enable_eager_celery(settings):
    settings.CELERY_TASK_ALWAYS_EAGER = True
    settings.CELERY_TASK_EAGER_PROPAGATES = True


@pytest.mark.django_db
class TestMonitoringAndCancelApi:
    def test_job_executions_pagination_and_filters(self, client):
        host1, _ = Host.objects.get_or_create(hostname="host-mon-1")
        host2, _ = Host.objects.get_or_create(hostname="host-mon-2")

        url = reverse("webhook_jobs")
        payload = {
            "external_id": "mon-id-1",
            "command_type": CommandType.PING,
            "hosts": ["host-mon-1", "host-mon-2"],
        }
        res_web = client.post(url, data=payload, content_type="application/json")
        job_id = res_web.json()["job_id"]

        # 1. Проверяем детальный статус задачи
        detail_url = reverse("job_detail", kwargs={"job_id": job_id})
        res_det = client.get(detail_url)
        assert res_det.status_code == 200
        assert res_det.json()["stats"]["total"] == 2

        # 2. Проверяем список executions с фильтром по статусу и пагинацией
        list_url = reverse("job_executions", kwargs={"job_id": job_id})
        res_list = client.get(f"{list_url}?status=SUCCESS&page=1")
        assert res_list.status_code == 200
        data = res_list.json()
        assert data["total_items"] == 2
        assert len(data["results"]) == 2

        # Проверяем отсутствие тяжелого поля logs в результатах списка
        assert "logs" not in data["results"][0]

    def test_execution_logs(self, client):
        Host.objects.get_or_create(hostname="host-logs-1")
        url = reverse("webhook_jobs")
        payload = {
            "external_id": "logs-id-1",
            "command_type": CommandType.PING,
            "hosts": ["host-logs-1"],
        }
        res = client.post(url, data=payload, content_type="application/json")
        job_id = res.json()["job_id"]

        list_url = reverse("job_executions", kwargs={"job_id": job_id})
        exec_id = client.get(list_url).json()["results"][0]["execution_id"]

        # Запрашиваем логи точечно
        logs_url = reverse("execution_logs", kwargs={"execution_id": exec_id})
        res_logs = client.get(logs_url)
        assert res_logs.status_code == 200
        assert "stdout: Success!" in res_logs.json()["logs"]

    def test_job_cancel_workflow_and_audit(self, client):
        Host.objects.get_or_create(hostname="host-cancel-1")
        url = reverse("webhook_jobs")
        payload = {
            "external_id": "cancel-id-1",
            "command_type": CommandType.DEPLOY,  # зависнет в WAIT_APPROVAL
            "hosts": ["host-cancel-1"],
        }
        res = client.post(url, data=payload, content_type="application/json")
        job_id = res.json()["job_id"]

        # Отменяем
        cancel_url = reverse("job_cancel", kwargs={"job_id": job_id})
        res_cancel = client.post(cancel_url)
        assert res_cancel.status_code == 200

        job = Job.objects.get(pk=job_id)
        assert job.status == JobStatus.CANCELLED

        # Проверяем создание Audit Log
        assert AuditLog.objects.filter(action="job_cancelled", details__job_id=job_id).exists()
