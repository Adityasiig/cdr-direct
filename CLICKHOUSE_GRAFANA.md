# ClickHouse and Grafana rollout

This stack adds ClickHouse and Grafana without removing the working CDR
dashboard. The existing DuckDB application remains available during ingestion
and validation.

## Data flow

1. 46Labs writes `/root/{entity}/YYYY/MM/DD/HH.csv.gz`.
2. `cdr-clickhouse-ingester` waits until an hourly file has been unchanged for
   ten minutes.
3. ClickHouse reads the gzip file directly from its read-only `user_files`
   mount and inserts the selected CDR columns in one batch.
4. A materialized view updates `cdr.cdr_hourly_media_ip`.
5. Grafana reads the hourly table for dashboards and `cdr.raw_cdr` only for the
   latest 1,000-row drill-down.

The required 46Labs header is `term_media_ip`. In this project that field is
displayed everywhere as **Termination Media IP**. It is the media IP selected
on the termination/vendor side of the call, not the incoming customer IP. A
file without this header is rejected and shown as an ingestion error rather
than silently loading incomplete analytics.

## Coolify deployment

Use `docker-compose.single-server.yml`.

Coolify generates these secrets automatically because the Compose file uses
its `SERVICE_PASSWORD_*` variables:

- `SERVICE_PASSWORD_CLICKHOUSEADMIN`
- `SERVICE_PASSWORD_CLICKHOUSEGRAFANA`
- `SERVICE_PASSWORD_GRAFANAADMIN`

Keep the existing `CDR_AUTH_TOKEN`. No ClickHouse or Grafana password needs to
be invented manually.

After pulling the new Git commit:

1. Click **Reload Compose File**.
2. Confirm the new services appear:
   - `clickhouse`
   - `cdr-clickhouse-ingester`
   - `grafana`
3. Assign a public domain only to `grafana`, using container port `3000`.
4. Do not assign a domain to ClickHouse ports `8123` or `9000`.
5. In **Environment Variables**, add the Termination Media IPs that must be
   watched. Commas, spaces, semicolons, and new lines are accepted:

   ```text
   CDR_WATCHED_TERM_MEDIA_IPS=203.0.113.10,198.51.100.20
   ```

6. Redeploy.
7. Open the Grafana service logs and wait for `HTTP Server Listen`.
8. Open the ingester logs and look for `INGESTED` messages and
   `Termination Media IP watchlist synchronized`.

Grafana login:

- Username: `admin`
- Password: the generated `SERVICE_PASSWORD_GRAFANAADMIN` value shown in
  Coolify's environment-variable view.

The provisioned dashboard is:

`CDR Analytics / CDR - Termination Media IP Monitor`

The dashboard intentionally omits revenue, profit, and general total-call KPI
cards. It shows each route in a direct order:

`Incoming Customer Trunk -> Termination Vendor -> Termination Trunk -> Termination Media IP`

`WATCHED IP ALERT` turns red when one or more configured IPs appear in the
selected time window. The adjacent match table shows exactly which source,
incoming trunk, termination vendor, termination trunk, signaling IP, and
Termination Media IP produced the match.

## Initial backfill

The safe first backfill is today plus yesterday:

```text
CDR_INGEST_LOOKBACK_DAYS=2
```

Files are loaded oldest first. With 100+ GB of compressed CDR per day, the
first backfill can take hours. Grafana begins showing data as soon as the first
hour is committed; it does not wait for the entire backfill.

After measuring the ClickHouse volume size, backfill more history by changing
`CDR_INGEST_LOOKBACK_DAYS` in Coolify and restarting only
`cdr-clickhouse-ingester`. Do not run multiple ingester replicas.

## Safety and deduplication

Each source path, size, and nanosecond modification time is recorded in
`cdr.ingest_log`.

- A matching completed file is skipped.
- An interrupted insert is recovered by checking the entity/hour already in
  ClickHouse.
- A file changed after ingestion is marked `changed` and is not inserted
  again. This prevents double-counting the materialized-view totals.

Changed files require a controlled entity/hour rebuild. Do not delete or
reinsert them manually while Grafana is in use.

## Storage

The ClickHouse volume has no automatic data deletion in this first phase.
Choose raw and aggregate retention after checking the raw server's available
NVMe/SSD capacity. The existing gzip files remain read-only and are never
modified by this stack.
