# Single-stage image — the Python bot and its FastAPI sidecar run in one process.
FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src

RUN pip install --no-cache-dir .

# SQLite lives here; the compose file mounts a host volume to persist it.
RUN mkdir -p /app/data
VOLUME ["/app/data"]

CMD ["python", "-m", "hall_monitor"]
