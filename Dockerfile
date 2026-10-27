# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY solarwatch ./solarwatch
COPY config.example.yaml config.demo.yaml docker-entrypoint.sh ./
RUN chmod 0755 docker-entrypoint.sh && mkdir -p /app/data

# Everything that changes lives in /app/data: config.yaml, the database, saved tokens.
ENV SOLARWATCH_CONFIG=/app/data/config.yaml \
    SOLARWATCH_HOST=0.0.0.0 \
    SOLARWATCH_PORT=8080 \
    TZ=Africa/Johannesburg \
    PUID=1000 \
    PGID=1000
VOLUME ["/app/data"]
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('SOLARWATCH_PORT', '8080'), timeout=4)" || exit 1

# The entrypoint creates config.yaml on first start, fixes data-folder ownership, then
# drops from root to PUID:PGID before starting the app.
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["python", "-m", "solarwatch"]
