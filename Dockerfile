FROM python:3.12.15-alpine3.24

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FORWARDED_ALLOW_IPS=127.0.0.1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apk upgrade --no-cache

COPY requirements.txt ./
RUN python -m pip install --upgrade 'pip>=26.2.2,<27' \
    && python -m pip install --only-binary=:all: -r requirements.txt \
    && python -m pip check \
    && python -m pip uninstall --yes pip \
    && python -c "import pathlib, shutil, sysconfig; shutil.rmtree(pathlib.Path(sysconfig.get_path('stdlib')) / 'ensurepip')"

COPY --chown=1000:1000 app ./app
COPY --chown=1000:1000 scripts/serve_shared.py ./scripts/serve_shared.py
COPY --chown=1000:1000 scripts/healthcheck.py ./scripts/healthcheck.py
COPY --chown=1000:1000 scripts/create_admin.py ./scripts/create_admin.py

RUN mkdir -p /data /exports \
    && addgroup -S -g 1000 sideword \
    && adduser -S -D -H -u 1000 -G sideword -h /app -s /sbin/nologin sideword \
    && chown sideword:sideword /data /exports

USER sideword

ENV SIDEWORD_DB_PATH=/data/sideword.sqlite3 \
    SIDEWORD_EXPORTS_DIR=/exports

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-m", "scripts.healthcheck"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--no-access-log", "--log-level", "warning", "--ws-max-size", "4096", "--limit-concurrency", "256"]
