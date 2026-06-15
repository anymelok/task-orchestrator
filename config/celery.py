import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("task_orchestrator")

app.config_from_object("django.conf:settings", namespace="CELERY")

# ищем задачи (tasks.py) в зарегистрированных приложениях
app.autodiscover_tasks()
