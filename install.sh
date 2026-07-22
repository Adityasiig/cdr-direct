#!/bin/bash
# CDR Direct - one-shot installer for 46labs (or any CentOS/RHEL CDR source box).
# Run as root.
#
# Steps:
#   1. DuckDB CLI binary -> /usr/local/bin/duckdb
#   2. pip install Python dependencies
#   3. /opt/cdr-direct application + /etc/cdr-direct-token (random 32-byte)
#   4. systemd unit + enable + start
#   5. loopback health check (use an SSH tunnel for dashboard access)

set -e

if [[ $EUID -ne 0 ]]; then
    echo "Must run as root" >&2
    exit 1
fi

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
DUCKDB_VERSION="${DUCKDB_VERSION:-v1.1.3}"

echo "[1/5] DuckDB CLI ${DUCKDB_VERSION}"
curl -sL -o /tmp/duckdb.zip \
    "https://github.com/duckdb/duckdb/releases/download/${DUCKDB_VERSION}/duckdb_cli-linux-amd64.zip"
python3 -c "import zipfile; zipfile.ZipFile('/tmp/duckdb.zip').extractall('/tmp/')"
mv -f /tmp/duckdb /usr/local/bin/duckdb
chmod +x /usr/local/bin/duckdb
rm -f /tmp/duckdb.zip
/usr/local/bin/duckdb --version

echo ""
echo "[2/5] Python deps"
pip3 install --quiet -r "${REPO_DIR}/requirements.txt"

echo ""
echo "[3/5] /opt/cdr-direct + token"
mkdir -p /opt/cdr-direct
install -m 0644 "${REPO_DIR}/api.py" "${REPO_DIR}/db.py" \
    "${REPO_DIR}/cache.py" "${REPO_DIR}/settings.py" /opt/cdr-direct/
install -m 0755 "${REPO_DIR}/backfill.py" "${REPO_DIR}/nightly_append.py" \
    "${REPO_DIR}/build_parquet.py" "${REPO_DIR}/cache_warmer.py" \
    "${REPO_DIR}/refresh_cache.py" /opt/cdr-direct/
mkdir -p /opt/cdr-direct/static /opt/cdr-direct/templates
install -m 0644 "${REPO_DIR}/static/"* /opt/cdr-direct/static/
install -m 0644 "${REPO_DIR}/templates/"* /opt/cdr-direct/templates/
if [[ ! -f /etc/cdr-direct.env ]]; then
    install -m 0640 "${REPO_DIR}/config.example.env" /etc/cdr-direct.env
fi
if [[ ! -f /etc/cdr-direct-token ]]; then
    python3 -c "import secrets; print(secrets.token_urlsafe(32))" > /etc/cdr-direct-token
    chmod 600 /etc/cdr-direct-token
    echo "Token generated:"
    cat /etc/cdr-direct-token
else
    echo "Token already exists at /etc/cdr-direct-token (not regenerated)"
fi

echo ""
echo "[4/5] systemd unit"
cp "${REPO_DIR}/cdr-direct.service" /etc/systemd/system/cdr-direct.service
systemctl daemon-reload
systemctl enable cdr-direct
systemctl restart cdr-direct
sleep 2
systemctl status cdr-direct --no-pager | head -8

echo ""
echo "[5/5] Loopback-only health check"

echo ""
echo "Health check:"
curl -s http://127.0.0.1:8090/health
echo ""
echo ""
echo "DONE. Dashboard is bound to 127.0.0.1:8090. Use an SSH tunnel."
echo "Token: $(cat /etc/cdr-direct-token)"
