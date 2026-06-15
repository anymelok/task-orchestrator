from typing import TYPE_CHECKING

from django.db import models

if TYPE_CHECKING:
    from django.db.models import Manager


class CommandType(models.TextChoices):
    PING = 'PING', 'Ping'
    RESTART_SERVICE = 'RESTART_SERVICE', 'Restart Service'
    DEPLOY = 'DEPLOY', 'Deploy'
    RUN_SCRIPT = 'RUN_SCRIPT', 'Run Script'


class JobStatus(models.TextChoices):
    NEW = 'NEW', 'New'
    WAIT_APPROVAL = 'WAIT_APPROVAL', 'Wait Approval'
    QUEUED = 'QUEUED', 'Queued'
    RUNNING = 'RUNNING', 'Running'
    SUCCESS = 'SUCCESS', 'Success'
    FAILED = 'FAILED', 'Failed'
    CANCELLED = 'CANCELLED', 'Cancelled'


class ExecutionStatus(models.TextChoices):
    NEW = 'NEW', 'New'
    WAIT_APPROVAL = 'WAIT_APPROVAL', 'Wait Approval'
    QUEUED = 'QUEUED', 'Queued'
    RUNNING = 'RUNNING', 'Running'
    SUCCESS = 'SUCCESS', 'Success'
    FAILED = 'FAILED', 'Failed'
    CANCELLED = 'CANCELLED', 'Cancelled'
    TIMEOUT = 'TIMEOUT', 'Timeout'
    BLOCKED = 'BLOCKED', 'Blocked'


class ApprovalStatus(models.TextChoices):
    PENDING = 'PENDING', 'Pending'
    APPROVED = 'APPROVED', 'Approved'
    REJECTED = 'REJECTED', 'Rejected'


class Host(models.Model):
    hostname = models.CharField(max_length=255, unique=True, db_index=True)
    metadata = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    if TYPE_CHECKING:
        executions: Manager['Execution']
        blocks: Manager['HostBlock']

    def __str__(self):
        return self.hostname


class HostBlock(models.Model):
    host = models.ForeignKey(Host, on_delete=models.CASCADE, related_name='blocks')
    command_type = models.CharField(max_length=50, choices=CommandType.choices)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('host', 'command_type')

    def __str__(self):
        return f'Block {self.command_type} on {self.host.hostname}'


class Job(models.Model):
    # external_id используется как ключ идемпотентности
    external_id = models.CharField(max_length=255, unique=True, db_index=True)
    command_type = models.CharField(max_length=50, choices=CommandType.choices)
    payload = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=20, choices=JobStatus.choices, default=JobStatus.NEW)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    if TYPE_CHECKING:
        executions: Manager['Execution']

    def __str__(self):
        return f'Job {self.pk} ({self.command_type}) - {self.status}'


class Execution(models.Model):
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name='executions')
    host = models.ForeignKey(Host, on_delete=models.CASCADE, related_name='executions')
    status = models.CharField(max_length=20, choices=ExecutionStatus.choices, default=ExecutionStatus.NEW)
    retry_count = models.IntegerField(default=0)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    timeout_seconds = models.IntegerField(default=300)
    logs = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # Индекс для ускорения выборки логов и статусов по связке
        indexes = [
            models.Index(fields=['job', 'status']),
            models.Index(fields=['host', 'status']),
        ]

    def __str__(self):
        return f'Execution {self.pk} for Job {self.job.pk} on {self.host.hostname} ({self.status})'


class Approval(models.Model):
    job = models.OneToOneField(Job, on_delete=models.CASCADE, related_name='approval')
    status = models.CharField(max_length=20, choices=ApprovalStatus.choices, default=ApprovalStatus.PENDING)
    resolved_by = models.CharField(max_length=150, null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'Approval for Job {self.job.pk} - {self.status}'


class AuditLog(models.Model):
    action = models.CharField(max_length=255)
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'[{self.created_at}] {self.action}'
