import pytest
from django.urls import reverse

from orchestrator.models import CommandType, Host


@pytest.fixture(autouse=True)
def enable_eager_celery(settings):
    settings.CELERY_TASK_ALWAYS_EAGER = True
    settings.CELERY_TASK_EAGER_PROPAGATES = True


@pytest.mark.django_db
class TestWebhookValidationAndIdempotency:
    def test_webhook_success_with_hosts(self, client):
        Host.objects.get_or_create(hostname="host-test-1")
        url = reverse("webhook_jobs")
        payload = {
            "external_id": "ext-id-1",
            "command_type": CommandType.PING,
            "hosts": ["host-test-1"],
        }
        res = client.post(url, data=payload, content_type="application/json")
        assert res.status_code == 201
        assert "job_id" in res.json()

    def test_webhook_success_with_selector(self, client):
        Host.objects.get_or_create(hostname="host-test-2", metadata={"dc": "ams"})
        url = reverse("webhook_jobs")
        payload = {
            "external_id": "ext-id-2",
            "command_type": CommandType.PING,
            "selector": {"dc": "ams"},
        }
        res = client.post(url, data=payload, content_type="application/json")
        assert res.status_code == 201

    def test_webhook_idempotency(self, client):
        Host.objects.get_or_create(hostname="host-test-3")
        url = reverse("webhook_jobs")
        payload = {
            "external_id": "ext-id-3",
            "command_type": CommandType.PING,
            "hosts": ["host-test-3"],
        }
        res1 = client.post(url, data=payload, content_type="application/json")
        assert res1.status_code == 201
        job_id = res1.json()["job_id"]

        res2 = client.post(url, data=payload, content_type="application/json")
        assert res2.status_code == 200
        assert res2.json()["job_id"] == job_id
        assert "already exists" in res2.json()["message"]

    def test_webhook_missing_external_id(self, client):
        url = reverse("webhook_jobs")
        payload = {"command_type": CommandType.PING, "hosts": ["host-test-1"]}
        res = client.post(url, data=payload, content_type="application/json")
        assert res.status_code == 400
        assert "Missing 'external_id'" in res.json()["error"]

    def test_webhook_invalid_command_type(self, client):
        url = reverse("webhook_jobs")
        payload = {
            "external_id": "ext-id-4",
            "command_type": "INVALID_CMD",
            "hosts": ["host-test-1"],
        }
        res = client.post(url, data=payload, content_type="application/json")
        assert res.status_code == 400
        assert "Invalid or missing 'command_type'" in res.json()["error"]

    def test_webhook_missing_targets(self, client):
        url = reverse("webhook_jobs")
        payload = {"external_id": "ext-id-5", "command_type": CommandType.PING}
        res = client.post(url, data=payload, content_type="application/json")
        assert res.status_code == 400
        assert "Either 'hosts' or 'selector'" in res.json()["error"]

    def test_webhook_invalid_json(self, client):
        url = reverse("webhook_jobs")
        res = client.post(url, data="invalid-non-json-string", content_type="application/json")
        assert res.status_code == 400
        assert "Invalid JSON" in res.json()["error"]
