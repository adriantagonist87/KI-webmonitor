FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends libcairo2 fonts-dejavu-core && rm -rf /var/lib/apt/lists/*
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
ENV HOME=/tmp ANONYMIZED_TELEMETRY=false DO_NOT_TRACK=1 BROWSER_USE_LOGGING_LEVEL=error
COPY app/requirements.lock /tmp/requirements.lock
RUN pip install -r /tmp/requirements.lock && pip check && pip freeze > /requirements.lock
WORKDIR /app
COPY app/ /app/
COPY scripts/backup_archive.py /app/backup_archive.py
COPY scripts/run-checks.py /app/run-checks.py
RUN chmod -R a+rX /app && mkdir -p /data /config /secrets /source/data /source/config /source/secrets /source/browser && chown -R 1000:1000 /data /config /secrets /source && chmod 0700 /data /config /secrets /source/* \
    && install -d -o 1000 -g 1000 -m 0700 /run/kiwm-page-reader
USER 1000:1000
HEALTHCHECK --interval=15s --timeout=8s --start-period=20s --retries=5 CMD ["python", "/app/healthcheck.py"]
CMD ["python", "main.py"]
