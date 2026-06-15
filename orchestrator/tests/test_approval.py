import pytest
from django.urls import reverse

from orchestrator.models import (
    ApprovalStatus,
    CommandType,
    ExecutionStatus,
    Host,
    Job,
    JobStatus,
)


@pytest.fixture(autouse=True)
def enable_eager_celery(settings):
    settings.CELERY_TASK_ALWAYS_EAGER = True
    settings.CELERY_TASK_EAGER_PROPAGATES = True


@pytest.mark.django_db
class TestApprovalWorkflow:
    def test_approval_workflow_approve(self, client):
        host, _ = Host.objects.get_or_create(hostname="host-deploy-1")
        url = reverse("webhook_jobs")
        payload = {
            "external_id": "approval-id-1",
            "command_type": CommandType.DEPLOY,  # требует аппрува
            "hosts": ["host-deploy-1"],
        }
        res = client.post(url, data=payload, content_type="application/json")
        assert res.status_code == 201
        job_id = res.json()["job_id"]

        # Проверяем зависание в WAIT_APPROVAL
        job = Job.objects.get(pk=job_id)
        assert job.status == JobStatus.WAIT_APPROVAL
        assert job.executions.count() == 0

        # Одобряем
        approve_url = reverse("job_approve", kwargs={"job_id": job_id})
        res_approve = client.post(approve_url)
        assert res_approve.status_code == 200

        # Проверяем успешный запуск и завершение после аппрува
        job.refresh_from_db()
        assert job.status == JobStatus.SUCCESS
        assert job.executions.filter(host=host, status=ExecutionStatus.SUCCESS).exists()

    def test_approval_workflow_reject(self, client):
        Host.objects.get_or_create(hostname="host-deploy-2")
        url = reverse("webhook_jobs")
        payload = {
            "external_id": "approval-id-2",
            "command_type": CommandType.DEPLOY,
            "hosts": ["host-deploy-2"],
        }
        res = client.post(url, data=payload, content_type="application/json")
        job_id = res.json()["job_id"]

        # Отклоняем
        reject_url = reverse("job_reject", kwargs={"job_id": job_id})
        res_reject = client.post(reject_url)
        assert res_reject.status_code == 200

        job = Job.objects.get(pk=job_id)
        assert job.status == JobStatus.CANCELLED
        assert job.approval.status == ApprovalStatus.REJECTED

    def test_approve_non_existent_approval(self, client):
        approve_url = reverse("job_approve", kwargs={"job_id": 99999})
        res = client.post(approve_url)
        assert res.status_code == 404

    def test_approve_already_approved(self, client):
        Host.objects.get_or_create(hostname="host-deploy-3")
        url = reverse("webhook_jobs")
        payload = {
            "external_id": "approval-id-3",
            "command_type": CommandType.DEPLOY,
            "hosts": ["host-deploy-3"],
        }
        res = client.post(url, data=payload, content_type="application/json")
        job_id = res.json()["job_id"]

        approve_url = reverse("job_approve", kwargs={"job_id": job_id})
        client.post(approve_url)  # первый раз
        res_second = client.post(approve_url)  # второй раз
        assert res_second.status_code == 400
        assert "already in state" in res_second.json()["error"]
