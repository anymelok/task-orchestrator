import abc
import random
import time

from django.conf import settings
from django.utils.module_loading import import_string

# Исключения агента


class AgentError(Exception):
    pass


class AgentConnectionError(AgentError):
    """ошибка подключения к хосту (сетевые проблемы)"""

    pass


class AgentTimeoutError(AgentError):
    """превышено время ожидания ответа от агента"""

    pass


class AgentExecutionError(AgentError):
    """агент ответил, но команда завершилась с ошибкой"""

    def __init__(self, message: str, logs: str = ''):
        super().__init__(message)
        self.logs = logs


# Интерфейс агента


class BaseAgentClient(abc.ABC):
    """
    Абстрактный класс клиента для взаимодействия с агентами на хостах
    Любой реальный клиент должен наследоваться от него
    """

    @abc.abstractmethod
    def execute(self, hostname: str, command_type: str, payload: dict, timeout: int) -> str:
        """
        Отправляет команду на исполнение агенту и дожидается результата (лог)

        :param hostname: имя хоста для подключения
        :param command_type: Тип команды (PING, RESTART_SERVICE)
        :param payload: Словарь с аргументами команды
        :param timeout: Ограничение времени выполнения в секундах
        :return: Строка с логами/выводом команды
        :raises AgentError: При сетевых сбоях, таймаутах или ошибках выполнения
        """
        pass


class MockAgentClient(BaseAgentClient):
    """
    Симулятор агента. Имитирует реальное поведение нестабильной сети и хостов
    """

    def execute(self, hostname: str, command_type: str, payload: dict, timeout: int) -> str:

        from django.conf import settings

        if getattr(settings, 'TESTING', False):
            return (
                f'stdout: Connection established with {hostname}\n'
                f"stdout: Executing command '{command_type}' (TEST MODE)\n"
                f'stdout: Success! Elapsed time: 0.00s.'
            )

        # сетевая задержка
        # latency = random.uniform(0.5, 3.0)

        # сильная задержка:
        latency = random.uniform(5.0, 10.0)
        time.sleep(latency)

        # обрыв соединения
        if random.random() < 0.10:
            raise AgentConnectionError(f"Connection lost with host '{hostname}' during executing '{command_type}'")

        # таймаут
        if random.random() < 0.05 or latency > timeout:
            raise AgentTimeoutError(
                f"Execution timed out on '{hostname}' (limit {timeout}s, actual latency {latency:.2f}s)"
            )

        # ошибка на хосте
        if random.random() < 0.10:
            error_logs = (
                f'stderr: Starting {command_type} with payload {payload}...\n'
                f'stderr: [CRITICAL] Internal service error\n'
                f'stderr: Command exited with non-zero status code (exit code: 1)'
            )
            raise AgentExecutionError(
                message=f"Command '{command_type}' failed on host '{hostname}'",
                logs=error_logs,
            )

        # Успех
        success_logs = (
            f'stdout: Connection established with {hostname}\n'
            f"stdout: Executing command '{command_type}'\n"
            f'stdout: Arguments: {payload}\n'
            f'stdout: [INFO] Progress 100%\n'
            f'stdout: Success! Elapsed time: {latency:.2f}s'
        )
        return success_logs


def get_agent_client() -> BaseAgentClient:
    """
    Динамически импортирует и возвращает экземпляр клиента,
    указанного в настройках AGENT_CLIENT_CLASS
    """
    client_path = getattr(settings, 'AGENT_CLIENT_CLASS', 'orchestrator.agent.MockAgentClient')
    client_class = import_string(client_path)
    return client_class()
