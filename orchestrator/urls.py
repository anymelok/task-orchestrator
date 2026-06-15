from django.urls import path

from . import views

urlpatterns = [
    # Webhook приема задач
    path("webhook/jobs", views.webhook_jobs, name="webhook_jobs"),
    # Approval Flow API
    path("jobs/<int:job_id>/approve", views.job_approve, name="job_approve"),
    path("jobs/<int:job_id>/reject", views.job_reject, name="job_reject"),
    # Host Blocks API
    path("hosts/<int:host_id>/blocks", views.host_blocks, name="host_blocks"),
    path(
        "hosts/<int:host_id>/blocks/<str:command_type>",
        views.host_block_delete,
        name="host_block_delete",
    ),
    # API мониторинга выполнения
    path("jobs/<int:job_id>", views.job_detail, name="job_detail"),
    path("jobs/<int:job_id>/executions", views.job_executions, name="job_executions"),
    path(
        "executions/<int:execution_id>/logs",
        views.execution_logs,
        name="execution_logs",
    ),
    # отмена джобы
    path("jobs/<int:job_id>/cancel", views.job_cancel, name="job_cancel"),
]
