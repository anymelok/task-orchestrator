# Дефолтные значения переменных, если они не переданы из консоли
USERS ?= 150
RATE ?= 10

.PHONY: help lint lint-fix format fix typecheck test check load-test

help:
	@echo "Доступные команды:"
	@echo "  make lint      - Проверка кода линтером Ruff"
	@echo "  make lint-fix  - Автоматическое исправление ошибок линтером Ruff"
	@echo "  make format    - Форматирование кода через Ruff"
	@echo "  make fix       - Комплексное автоисправление (линтер --fix + форматирование)"
	@echo "  make typecheck - Статический анализ типов через Mypy"
	@echo "  make test      - Запуск тестов с замером покрытия"
	@echo "  make check     - Запуск всех проверок (линтер + типы + тесты)"
	@echo "  make load-test - Запуск нагрузочного теста в Headless-режиме (Locust)"
	@echo "                   Можно передать параметры: make load-test USERS=300 RATE=20"

lint:
	uv run ruff check orchestrator config

lint-fix:
	uv run ruff check --fix orchestrator config

format:
	uv run ruff format orchestrator config

fix: lint-fix format

typecheck:
	uv run mypy orchestrator

test:
	@echo "Проверка готовности Redis и MySQL в Docker..."
	@docker compose up -d redis db
	@echo "Запуск тестов..."
	uv run pytest -v --cov=orchestrator --cov-report=term-missing

check: lint typecheck test

load-test:
	@chmod +x run_load_test.sh
	./run_load_test.sh $(USERS) $(RATE)
