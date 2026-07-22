"""
DuckDB native database for fast CDR queries.
Schema covers the ~30 columns actually used by the dashboard;
zone maps + clustering on (entity, call_end_time) give 10-30× speedup
over read_csv_auto on raw .csv.gz files.
"""
import os
import subprocess
import json
from datetime import datetime, timedelta

from settings import DB_PATH, DUCKDB_BIN, DUCKDB_THREADS, DUCKDB_MEMORY_LIMIT

# Columns we keep — slim schema, ~30 of the 80 CSV columns
KEEP_COLUMNS = [
    'sip_code', 'sip_reason', 'reason',
    'call_start_time', 'call_end_time', 'call_answered_time',
    'from_did', 'to_did', 'lrn_did', 'callid',
    'duration_real',
    'orig_carrier_name', 'orig_trunk_group_name', 'orig_juris',
    'orig_rate', 'orig_billed_duration', 'orig_cost',
    'term_carrier_name', 'term_trunk_group_name', 'term_juris',
    'term_rate', 'term_billed_duration', 'term_cost',
    'orig_state', 'orig_ratecenter', 'orig_destination',
    'term_state', 'term_ratecenter', 'term_destination',
    'orig_billed_prefix', 'term_billed_prefix',
    'leg',
    'stir_x5u', 'stir_attest',
]

# Type overrides for read_csv_auto — to_did especially can overflow BIGINT
TYPE_OVERRIDES = {
    'to_did': 'VARCHAR',
    'from_did': 'VARCHAR',
    'lrn_did': 'VARCHAR',
    'callid': 'VARCHAR',
    'orig_carrier_id': 'VARCHAR',
    'term_carrier_id': 'VARCHAR',
    'stir_x5u': 'VARCHAR',
    'stir_attest': 'VARCHAR',
    'stir_orig_id': 'VARCHAR',
    'stir_orig_tn': 'VARCHAR',
    'stir_dest_tn': 'VARCHAR',
    'p_charge_info': 'VARCHAR',
}


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS cdr_records (
    entity              VARCHAR NOT NULL,
    file_hour           TIMESTAMP NOT NULL,

    -- timestamps
    call_start_time     TIMESTAMP,
    call_end_time       TIMESTAMP,
    call_answered_time  TIMESTAMP,

    -- SIP outcome
    sip_code            INTEGER,
    sip_reason          VARCHAR,
    reason              VARCHAR,   -- granular switch termination reason (CSV col 36):
                                   -- NO_VENDOR_RATE / NO_CUSTOMER_RATE / LIMITS_EXCEEDED / etc.

    -- DIDs / call identifiers (VARCHAR to survive overflow)
    from_did            VARCHAR,
    to_did              VARCHAR,
    lrn_did             VARCHAR,
    callid              VARCHAR,

    -- duration / call shape
    duration_real       INTEGER,
    leg                 VARCHAR,

    -- origination (customer) side
    orig_carrier_name       VARCHAR,
    orig_trunk_group_name   VARCHAR,
    orig_juris              VARCHAR,
    orig_rate               DOUBLE,
    orig_billed_duration    INTEGER,
    orig_cost               DOUBLE,
    orig_state              VARCHAR,
    orig_ratecenter         VARCHAR,
    orig_destination        VARCHAR,
    orig_billed_prefix      VARCHAR,

    -- termination (vendor) side
    term_carrier_name       VARCHAR,
    term_trunk_group_name   VARCHAR,
    term_juris              VARCHAR,
    term_rate               DOUBLE,
    term_billed_duration    INTEGER,
    term_cost               DOUBLE,
    term_state              VARCHAR,
    term_ratecenter         VARCHAR,
    term_destination        VARCHAR,
    term_billed_prefix      VARCHAR,
    stir_x5u                VARCHAR,
    stir_attest             VARCHAR
);

CREATE TABLE IF NOT EXISTS ingest_log (
    entity         VARCHAR NOT NULL,
    file_hour      TIMESTAMP NOT NULL,
    csv_path       VARCHAR,
    rows_ingested  BIGINT,
    ingested_at    TIMESTAMP,
    source_size    BIGINT,
    source_mtime_ns BIGINT,
    PRIMARY KEY (entity, file_hour)
);
"""


def run_query(sql, timeout=600, threads=None, memory_limit_gb=None):
    """
    Run a query against the native DuckDB file. Returns (rows, error).

    threads=24 caps DuckDB parallelism; memory_limit prevents 268M-row queries
    from consuming all 251 GB RAM and triggering kswapd thrashing (load 50+).
    With memory_limit, DuckDB spills to disk instead of OOM-thrashing.
    """
    threads = threads or DUCKDB_THREADS
    memory_limit = '{}GB'.format(memory_limit_gb) if memory_limit_gb else DUCKDB_MEMORY_LIMIT
    pragmas = "PRAGMA threads={}; PRAGMA memory_limit='{}'; ".format(threads, memory_limit)
    threaded_sql = pragmas + sql
    try:
        result = subprocess.run(
            [DUCKDB_BIN, '-json', DB_PATH, '-c', threaded_sql],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=timeout,
        )
        if result.returncode != 0:
            return None, result.stderr.strip()[:500]
        text = result.stdout.strip() or '[]'
        return json.loads(text), None
    except subprocess.TimeoutExpired:
        return None, 'query timeout ({}s max)'.format(timeout)
    except OSError as e:
        return None, 'duckdb unavailable: {}'.format(e)
    except ValueError as e:
        return None, 'json parse error: {}'.format(e)


def init():
    """Create schema if not exists."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    rows, err = run_query(SCHEMA_SQL, timeout=60)
    if err:
        raise RuntimeError('schema init failed: ' + err)
    # Idempotent migration: existing DB files predate the `reason` column.
    # ADD COLUMN IF NOT EXISTS is instant (no row rewrite); existing rows read
    # NULL for reason until re-ingested. New ingests populate it via KEEP_COLUMNS.
    _, merr = run_query(
        "ALTER TABLE cdr_records ADD COLUMN IF NOT EXISTS reason VARCHAR;", timeout=60
    )
    if merr and 'already exists' not in merr.lower():
        print('[startup] reason-column migration warning:', merr)
    for column_sql in (
        "ALTER TABLE ingest_log ADD COLUMN IF NOT EXISTS source_size BIGINT;",
        "ALTER TABLE ingest_log ADD COLUMN IF NOT EXISTS source_mtime_ns BIGINT;",
    ):
        _, migration_error = run_query(column_sql, timeout=60)
        if migration_error and 'already exists' not in migration_error.lower():
            print('[startup] ingest-log migration warning:', migration_error)


def ingest_csv_file(csv_path, entity, file_hour_iso):
    """
    Bulk-load one CSV.gz file into cdr_records, then log it.
    Returns (rows_ingested, error).
    """
    if entity not in ('MyCallConnect', 'SalamTalk', 'Dialphone', 'Vestacall'):
        return 0, 'unknown entity'
    try:
        source_stat = os.stat(csv_path)
    except OSError as exc:
        return 0, 'source file unavailable: {}'.format(exc)

    safe_entity = entity.replace("'", "''")
    safe_csv = os.path.abspath(csv_path).replace("'", "''")

    # Explicit target columns matching CREATE TABLE order
    target_cols = ['entity', 'file_hour'] + KEEP_COLUMNS
    # SELECT in same order: literal entity, literal file_hour, then named cols from CSV
    select_cols = ["'{}' AS entity".format(safe_entity),
                   "TIMESTAMP '{}' AS file_hour".format(file_hour_iso)]
    select_cols += KEEP_COLUMNS  # CSV columns referenced by name
    type_overrides = ", ".join(["'{}': '{}'".format(k, v) for k, v in TYPE_OVERRIDES.items()])

    sql = """
    BEGIN TRANSACTION;

    DELETE FROM cdr_records
      WHERE entity = '{entity}' AND file_hour = TIMESTAMP '{file_hour}';

    INSERT INTO cdr_records ({target})
      SELECT {select}
      FROM read_csv_auto(
        ['{csv}'],
        union_by_name=true,
        ignore_errors=true,
        types={{ {types} }}
      );

    INSERT OR REPLACE INTO ingest_log
      (entity, file_hour, csv_path, rows_ingested, ingested_at, source_size, source_mtime_ns)
      VALUES (
        '{entity}',
        TIMESTAMP '{file_hour}',
        '{csv}',
        (SELECT COUNT(*) FROM cdr_records WHERE entity = '{entity}' AND file_hour = TIMESTAMP '{file_hour}'),
        CURRENT_TIMESTAMP,
        {source_size},
        {source_mtime_ns}
      );

    COMMIT;
    """.format(
        entity=entity, file_hour=file_hour_iso,
        target=', '.join(target_cols),
        select=', '.join(select_cols),
        csv=safe_csv,
        types=type_overrides,
        source_size=source_stat.st_size,
        source_mtime_ns=source_stat.st_mtime_ns,
    )

    rows, err = run_query(sql, timeout=600)
    if err:
        return 0, err
    cnt_rows, _ = run_query(
        "SELECT rows_ingested AS c FROM ingest_log "
        "WHERE entity = '{}' AND file_hour = TIMESTAMP '{}'".format(entity, file_hour_iso)
    )
    if cnt_rows and len(cnt_rows) > 0:
        return cnt_rows[0].get('c') or 0, None
    return 0, None


def already_ingested(entity, file_hour_iso, csv_path=None):
    safe_entity = entity.replace("'", "''")
    rows, err = run_query(
        "SELECT csv_path, source_size, source_mtime_ns FROM ingest_log "
        "WHERE entity = '{}' AND file_hour = TIMESTAMP '{}'".format(
            safe_entity, file_hour_iso
        )
    )
    if not rows or err:
        return False
    if not csv_path:
        return True
    try:
        source_stat = os.stat(csv_path)
    except OSError:
        return False
    row = rows[0]
    same_path = os.path.abspath(row.get('csv_path') or '') == os.path.abspath(csv_path)
    if row.get('source_size') is None or row.get('source_mtime_ns') is None:
        # Upgrade legacy log rows without forcing a duplicate ingest.
        if same_path:
            run_query(
                "UPDATE ingest_log SET source_size={}, source_mtime_ns={} "
                "WHERE entity='{}' AND file_hour=TIMESTAMP '{}'".format(
                    source_stat.st_size, source_stat.st_mtime_ns,
                    safe_entity, file_hour_iso,
                ),
                timeout=30,
            )
            return True
        return False
    return (
        same_path
        and int(row['source_size']) == source_stat.st_size
        and int(row['source_mtime_ns']) == source_stat.st_mtime_ns
    )


def stats():
    """Return ingestion stats."""
    rows, err = run_query("""
      SELECT
        (SELECT COUNT(*) FROM cdr_records) AS total_rows,
        (SELECT COUNT(*) FROM ingest_log) AS files_ingested,
        (SELECT MIN(file_hour) FROM cdr_records) AS oldest_hour,
        (SELECT MAX(file_hour) FROM cdr_records) AS newest_hour,
        (SELECT COUNT(DISTINCT entity) FROM cdr_records) AS entity_count
    """)
    if err:
        return {'error': err}
    if not rows:
        return {'total_rows': 0, 'files_ingested': 0}
    s = rows[0]
    try:
        s['db_size_bytes'] = os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0
    except OSError:
        s['db_size_bytes'] = 0
    return s


def drop_older_than(cutoff_date_iso):
    """Remove rows + ingest_log entries older than cutoff (rolling window)."""
    sql = """
    DELETE FROM cdr_records WHERE file_hour < TIMESTAMP '{cutoff}';
    DELETE FROM ingest_log WHERE file_hour < TIMESTAMP '{cutoff}';
    """.format(cutoff=cutoff_date_iso)
    _, err = run_query(sql, timeout=300)
    return err is None
