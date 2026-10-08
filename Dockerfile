FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FORWARDED_ALLOW_IPS=127.0.0.1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY --chown=1000:1000 app ./app
COPY --chown=1000:1000 scripts/serve_shared.py ./scripts/serve_shared.py

RUN mkdir -p /data /exports \
    && useradd --system --uid 1000 --home /app sideword \
    && chown sideword:sideword /data /exports

USER sideword

ENV SIDEWORD_DB_PATH=/data/sideword.sqlite3 \
    SIDEWORD_EXPORTS_DIR=/exports

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--no-access-log", "--log-level", "warning", "--ws-max-size", "4096", "--limit-concurrency", "256"]
