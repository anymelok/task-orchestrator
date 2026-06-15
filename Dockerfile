FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock ./

RUN uv venv && uv sync --frozen --no-dev

COPY . .

EXPOSE 8000

# Команда по умолчанию (будет переопределена в docker-compose)
CMD ["uv", "run", "python", "manage.py", "runserver", "0.0.0.0:8000"]
