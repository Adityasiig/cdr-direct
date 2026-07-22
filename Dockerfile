FROM python:3.12-slim-bookworm

ARG DUCKDB_VERSION=1.5.4
ARG TARGETARCH

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CDR_ROOT=/data/raw \
    CDR_APP_DATA_ROOT=/data/runtime \
    CDR_PARQUET_ROOT=/data/parquet \
    CDR_DB_PATH=/data/runtime/cdrdata.duckdb \
    CDR_CACHE_DB=/data/runtime/cache.sqlite \
    CDR_DUCKDB_BIN=/usr/local/bin/duckdb \
    CDR_ENABLE_SQL=0

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl unzip; \
    case "${TARGETARCH:-amd64}" in amd64) duck_arch=amd64 ;; arm64) duck_arch=arm64 ;; *) exit 1 ;; esac; \
    curl -fsSL -o /tmp/duckdb.zip \
      "https://github.com/duckdb/duckdb/releases/download/v${DUCKDB_VERSION}/duckdb_cli-linux-${duck_arch}.zip"; \
    unzip /tmp/duckdb.zip -d /tmp/duckdb; \
    install -m 0755 /tmp/duckdb/duckdb /usr/local/bin/duckdb; \
    duckdb --version; \
    rm -rf /tmp/duckdb /tmp/duckdb.zip /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY api.py cache.py db.py settings.py ./
COPY static ./static
COPY templates ./templates
COPY backfill.py nightly_append.py build_parquet.py cache_warmer.py refresh_cache.py ./

RUN mkdir -p /data/raw /data/runtime /data/parquet

EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/ready', timeout=3)" || exit 1

CMD ["sh", "-c", "exec gunicorn --workers ${CDR_WEB_WORKERS:-1} --threads ${CDR_WEB_THREADS:-4} --bind 0.0.0.0:8090 --timeout 360 --access-logfile - --error-logfile - api:app"]
