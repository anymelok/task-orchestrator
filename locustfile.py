import random
import uuid

from locust import HttpUser, between, task


class OrchestratorLoadTestUser(HttpUser):
    # Пауза между запросами пользователя от 0.1 до 0.5 секунд
    wait_time = between(0.1, 0.5)

    @task(3)  # Вес задачи (выполняется чаще остальных)
    def send_webhook_job(self):
        """Эмулирует отправку вебхука от внешней системы"""
        # Генерируем уникальный external_id, чтобы не срабатывала дедупликация
        external_id = f'locust-{uuid.uuid4()}'

        payload = {
            'external_id': external_id,
            'command_type': random.choice(['PING', 'RESTART_SERVICE', 'RUN_SCRIPT']),
            'payload': {'param': random.randint(1, 100)},
            # Шлем на наш тестовый хост
            'hosts': ['host-docker-test'],
        }

        # Отправляем POST вебхук
        with self.client.post('/webhook/jobs', json=payload, catch_response=True) as response:
            if response.status_code == 201:
                response.success()
                # Сохраняем job_id в контекст пользователя для последующего чтения
                job_id = response.json().get('job_id')
                if job_id:
                    self.user_job_ids.append(job_id)
            else:
                response.failure(f'Failed to submit job: {response.text}')

    @task(1)
    def get_job_status(self):
        """Эмулирует проверку статуса задачи внешней системой"""
        if not self.user_job_ids:
            return

        # Берем случайную задачу из тех, что отправил этот пользователь
        job_id = random.choice(self.user_job_ids)
        self.client.get(f'/jobs/{job_id}', name='/jobs/{job_id}')

    @task(1)
    def get_job_executions(self):
        """Эмулирует детальный просмотр executions по хостам"""
        if not self.user_job_ids:
            return

        job_id = random.choice(self.user_job_ids)
        self.client.get(f'/jobs/{job_id}/executions', name='/jobs/{job_id}/executions')

    def on_start(self):
        """Вызывается при старте виртуального пользователя"""
        self.user_job_ids = []
