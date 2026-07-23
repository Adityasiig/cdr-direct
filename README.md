# CDR Direct

CDR Direct ingests hourly compressed 46Labs CDR files into DuckDB and serves a
small authenticated Flask dashboard for USA NPANXX, origin-trunk, quality, revenue,
cost, and margin analysis.

The dashboard's origin-trunk list, exact filter, grouping, and export all use
the raw CDR column `orig_trunk_group_name` (not `orig_carrier_name`).

The normal query path reads the native DuckDB store. Raw `.csv.gz` scanning is
kept only as a correctness fallback while a new or changed hourly file is still
waiting to be ingested.

## Data flow

1. 46Labs writes `{entity}/{YYYY}/{MM}/{DD}/{HH}.csv.gz`.
2. `nightly_append.py` ignores files modified in the last two minutes.
3. Each stable file is loaded transactionally into `cdr_records`.
4. `ingest_log` records the file path, size, nanosecond mtime, row count, and
   ingestion time. A changed file is replaced rather than duplicated.
5. API queries use DuckDB only when every source hour is present and unchanged;
   otherwise they fall back to the raw files.
6. `build_parquet.py --days 30` creates the partitioned compressed cold store.

## Correct metric behavior

The dashboard defaults to **all SIP outcomes**. Attempts therefore include
failures, completions include SIP 200 calls, and ASR is:

`100 × SIP-200 completions / all attempts in the selected scope`

Choosing SIP 200 alone intentionally describes completed-call traffic and will
produce 100% ASR. The FAS view automatically restores all outcomes before
querying. Sorting and quick views run on the backend, not merely on the first
5,000 rows shown in the browser.

## Configuration

All large-data paths are environment-driven; see `config.example.env`.
Existing Linux defaults remain compatible.

For a Windows D-drive deployment:

```text
CDR_ROOT=D:\CDR\data\raw
CDR_APP_DATA_ROOT=D:\CDR\runtime
CDR_PARQUET_ROOT=D:\CDR\data\processed\parquet
CDR_DB_PATH=D:\CDR\runtime\cdrdata.duckdb
CDR_CACHE_DB=D:\CDR\runtime\cache.sqlite
```

The source checkout can be elsewhere, but raw data, DuckDB, Parquet, and cache
files should remain on the high-capacity data drive.

Important controls:

- `CDR_MAX_QUERY_DAYS=31`
- `CDR_MAX_RESULT_ROWS=5000`
- `CDR_CSV_EXPORT_MAX_ROWS=100000` (legacy per-code/STIR exports; the main
  origin-trunk export streams without this row cap)
- `CDR_DUCKDB_THREADS=8`
- `CDR_DUCKDB_MEMORY_LIMIT=8GB`
- `CDR_WEB_TIMEOUT=1800` (allows large streamed exports up to 30 minutes)
- `CDR_ENABLE_SQL=0` (recommended; arbitrary SQL is not required by the UI)
- `CDR_AUTH_TOKEN=...` or `CDR_TOKEN_FILE=/etc/cdr-direct-token`

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness |
| GET | `/ui` | Dashboard |
| POST | `/api/usa-codes` | Per-NPANXX aggregates |
| POST | `/api/usa-customer-codes` | Per-origin-trunk/NPANXX aggregates |
| POST | `/api/usa-customer-codes/csv-ticket` | Create a signed 10-minute download link |
| GET | `/api/usa-customer-codes/csv` | Unlimited, direct DuckDB CSV stream |
| GET | `/api/cache-stats` | Cache diagnostics |
| GET | `/api/db-stats` | Ingestion diagnostics |
| POST | `/sql` | Disabled by default |

All API creation/query calls require `X-Auth-Token`. The CSV download GET uses
the short-lived signed ticket returned by the authenticated ticket endpoint.

## Development checks

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
node --check static\app.js
```

## Deployment

`install.sh` installs all Python modules, jobs, templates, and static assets.
The systemd service binds to `127.0.0.1:8090`; access the dashboard through an
SSH tunnel instead of exposing the billing API directly to the internet.

### Split deployment: data node plus Coolify

This is the recommended layout when the raw CDR server and Coolify server are
different machines:

```text
raw CDR server                      Coolify server
------------------------------      -----------------------------
raw .csv.gz files                   public HTTPS dashboard
DuckDB + cache on local NVMe  <---- allow-listed HTTPS API proxy
ingestion and all SQL queries       no DuckDB and no raw CDR files
```

No VPN is required. The raw server runs Caddy on ports 80/443 for automatic
HTTPS and accepts application requests only from the Coolify server's static
public IP. DuckDB port `8090` remains internal to Docker and is never published.
Do not mount the `.duckdb` file over NFS or SMB.

#### 1. Start the private data node on the raw CDR server

Copy this repository to `/opt/cdr-direct`, then:

```bash
cd /opt/cdr-direct
cp data-node.example.env .env
sudo mkdir -p /var/lib/cdr-direct/runtime /var/lib/cdr-direct/parquet
```

Edit `.env`:

- `CDR_API_DOMAIN` is a DNS hostname pointing directly to the raw server, such
  as `cdr-api.example.com`. Use DNS-only mode rather than a CDN proxy.
- `COOLIFY_PUBLIC_IP` is the Coolify server's static public IP in CIDR form,
  such as `203.0.113.10/32`.
- `CDR_BACKEND_TOKEN` must be a long random value used only between servers.
- `CDR_RAW_HOST_PATH` must be the actual raw CDR directory on this server.
- The 256 GB starting profile is `CDR_DUCKDB_THREADS=16` and
  `CDR_DUCKDB_MEMORY_LIMIT=64GB`.

Build and initialize it:

```bash
docker compose -f docker-compose.data-node.yml config
docker compose -f docker-compose.data-node.yml up -d --build
docker compose -f docker-compose.data-node.yml exec -T cdr-data-node python backfill.py 4
```

Install the ten-minute ingestion timer. Adjust `WorkingDirectory` in the
service if the repository is not at `/opt/cdr-direct`.

```bash
sudo cp cdr-data-ingest.service cdr-data-ingest.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cdr-data-ingest.timer
sudo systemctl start cdr-data-ingest.service
```

Point the API hostname's DNS A/AAAA record to the raw server and allow inbound
ports 80/443 to Caddy so it can issue and renew HTTPS certificates. Do not open
port 8090. Confirm from the Coolify host that
`https://cdr-api.example.com/health` is reachable. Requests from other source
IPs receive HTTP 403 before reaching the data node.

#### 2. Deploy the public dashboard on Coolify

Create a Docker Compose application using `docker-compose.coolify.yml`. Set
the variables shown in `coolify.example.env`:

- `CDR_BACKEND_URL=https://cdr-api.example.com`
- `CDR_BACKEND_TOKEN` is the same private token as the data node.
- `CDR_PUBLIC_USERNAME` and `CDR_PUBLIC_PASSWORD` are the dashboard login and
  must be different from the backend token.

Expose port `8090`, attach the public domain, enable HTTPS, use `/ready` as the
health check, and keep one replica. `/ready` verifies that Coolify can reach the
private data node. The proxy exposes only the dashboard API routes; `/sql` is
not forwarded. It injects the backend token on the server side, so the private
token is never stored in browser JavaScript or local storage.

The Coolify server now carries only aggregated JSON/CSV responses. All raw-file
reading, decompression, DuckDB work, temporary spill files, and cache generation
happen on the 256 GB raw CDR server.

`docker-compose.single-server.yml` preserves the older all-in-one deployment
for installations where Coolify and the raw files are on the same machine.

That single-server Compose file also starts `cdr-cache-warmer`. It prepares the
latest completed UTC day in the persistent SQLite cache every day. Before
02:00 UTC the dashboard keeps serving the previous completed day; after 02:00
UTC it refreshes yesterday once, then new browser sessions load that prepared
snapshot without scanning raw gzip files. The first deployment has one initial
40-90 second preparation; later page opens use the prepared result.

Cached requests should be fast, but an uncached query grouping hundreds of
millions of rows can still take minutes. Hourly/daily aggregate tables are the
next optimization if cold queries must consistently be interactive.

Real billing data, database files, caches, Parquet files, secrets, and local
virtual environments are excluded by `.gitignore`.
