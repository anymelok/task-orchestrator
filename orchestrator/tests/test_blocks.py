import pytest
from django.urls import reverse

from orchestrator.models import (
    CommandType,
    ExecutionStatus,
    Host,
    HostBlock,
    Job,
    JobStatus,
)


@pytest.fixture(autouse=True)
def enable_eager_celery(settings):
    settings.CELERY_TASK_ALWAYS_EAGER = True
    settings.CELERY_TASK_EAGER_PROPAGATES = True


@pytest.mark.django_db
class TestHostBlocks:
    def test_set_and_delete_blocks_via_api(self, client):
        host, _ = Host.objects.get_or_create(hostname="host-block-1")

        # 1. Устанавливаем блокировки
        blocks_url = reverse("host_blocks", kwargs={"host_id": host.pk})
        payload = {"command_types": [CommandType.PING, CommandType.RESTART_SERVICE]}
        res = client.put(blocks_url, data=payload, content_type="application/json")
        assert res.status_code == 200
        assert HostBlock.objects.filter(host=host).count() == 2

        # 2. Удаляем одну блокировку
        delete_url = reverse(
            "host_block_delete",
            kwargs={"host_id": host.pk, "command_type": CommandType.PING},
        )
        res_del = client.delete(delete_url)
        assert res_del.status_code == 200
        assert HostBlock.objects.filter(host=host).count() == 1

    def test_blocked_command_execution(self, client):
        host, _ = Host.objects.get_or_create(hostname="host-block-2")
        HostBlock.objects.create(host=host, command_type=CommandType.RESTART_SERVICE)

        url = reverse("webhook_jobs")
        payload = {
            "external_id": "block-exec-id-1",
            "command_type": CommandType.RESTART_SERVICE,
            "hosts": ["host-block-2"],
        }
        res = client.post(url, data=payload, content_type="application/json")
        job_id = res.json()["job_id"]

        job = Job.objects.get(pk=job_id)
        assert job.status == JobStatus.FAILED
        assert job.executions.filter(host=host, status=ExecutionStatus.BLOCKED).exists()

    def test_set_blocks_on_non_existent_host(self, client):
        blocks_url = reverse("host_blocks", kwargs={"host_id": 99999})
        res = client.put(
            blocks_url,
            data={"command_types": [CommandType.PING]},
            content_type="application/json",
        )
        assert res.status_code == 404

    def test_set_invalid_command_type_block(self, client):
        host, _ = Host.objects.get_or_create(hostname="host-block-3")
        blocks_url = reverse("host_blocks", kwargs={"host_id": host.pk})
        res = client.put(
            blocks_url,
            data={"command_types": ["INVALID"]},
            content_type="application/json",
        )
        assert res.status_code == 400
