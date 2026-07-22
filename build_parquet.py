#!/usr/bin/env python3
"""
Phase 1 — one-time Parquet backfill for the 30-day cold store.

Converts each raw hourly CSV.gz into a partitioned Parquet file whose schema
is byte-identical to the cdr_records hot table (entity, file_hour + KEEP_COLUMNS,
same TYPE_OVERRIDES). Read-only w.r.t. the live dashboard — writes a NEW tree at
/root/parquet and never touches cdr_records, the DuckDB file, or the raw gz.

Layout:  /root/parquet/{entity}/{YYYY-MM-DD}/{HH}.parquet
Resumable: skips any (entity, day, hour) whose .parquet already exists & is non-empty.
"""
import os
import re
import glob
import json
import time
import subprocess
from datetime import datetime, timedelta, timezone

from settings import CDR_ROOT, PARQUET_ROOT, DUCKDB_BIN as DUCKDB

ENTITIES = ('MyCallConnect', 'SalamTalk', 'Dialphone', 'Vestacall')

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
TYPE_OVERRIDES = {
    'to_did': 'VARCHAR', 'from_did': 'VARCHAR', 'lrn_did': 'VARCHAR', 'callid': 'VARCHAR',
    'orig_carrier_id': 'VARCHAR', 'term_carrier_id': 'VARCHAR',
    'stir_x5u': 'VARCHAR', 'stir_attest': 'VARCHAR', 'stir_orig_id': 'VARCHAR',
    'stir_orig_tn': 'VARCHAR', 'stir_dest_tn': 'VARCHAR', 'p_charge_info': 'VARCHAR',
}


def log(*a):
    print(time.strftime('%Y-%m-%d %H:%M:%S'), '[parquet]', *a, flush=True)


def run(sql, timeout=600):
    r = subprocess.run([DUCKDB, '-json', '-c', sql],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       universal_newlines=True, timeout=timeout, cwd=CDR_ROOT)
    if r.returncode != 0:
        return None, r.stderr.strip()[:400]
    return json.loads(r.stdout.strip() or '[]'), None


def convert_one(entity, day_iso, hour, csv_path):
    out_dir = os.path.join(PARQUET_ROOT, entity, day_iso)
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, '{:02d}.parquet'.format(hour))
    if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        return 'skip', 0
    file_hour_iso = '{} {:02d}:00:00'.format(day_iso, hour)
    select_cols = ["'{}' AS entity".format(entity.replace("'", "''")),
                   "TIMESTAMP '{}' AS file_hour".format(file_hour_iso)]
    select_cols += KEEP_COLUMNS
    types = ", ".join("'{}': '{}'".format(k, v) for k, v in TYPE_OVERRIDES.items())
    tmp_path = out_path + '.tmp'
    sql = """
    COPY (
      SELECT {select}
      FROM read_csv_auto(['{csv}'], union_by_name=true, ignore_errors=true,
                         types={{ {types} }})
    ) TO '{out}' (FORMAT PARQUET, COMPRESSION 'zstd', ROW_GROUP_SIZE 200000);
    """.format(select=', '.join(select_cols), csv=csv_path, types=types, out=tmp_path)
    _, err = run(sql)
    if err:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        return 'error', err
    os.rename(tmp_path, out_path)
    cnt, err = run("SELECT COUNT(*) AS n FROM read_parquet('{}')".format(out_path))
    n = cnt[0]['n'] if cnt else 0
    return 'ok', n


def source_metadata(csv_path):
    """Return (entity, YYYY-MM-DD, hour) for a canonical source path."""
    try:
        parts = os.path.relpath(os.path.abspath(csv_path), CDR_ROOT).split(os.sep)
        if len(parts) != 5 or parts[0] not in ENTITIES:
            return None
        entity, year, month, day, filename = parts
        match = re.match(r'^(\d{2})\.csv\.gz$', filename)
        parsed_day = datetime(int(year), int(month), int(day)).date()
        hour = int(match.group(1)) if match else -1
        if not 0 <= hour <= 23:
            return None
        return entity, parsed_day.isoformat(), hour
    except (OSError, TypeError, ValueError):
        return None


def main(days=30):
    t0 = time.time()
    total_ok = total_skip = total_err = total_rows = 0
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=max(1, days) - 1)
    for entity in ENTITIES:
        files = sorted(glob.glob(os.path.join(CDR_ROOT, entity, '*', '*', '*', '*.csv.gz')))
        files = [path for path in files if source_metadata(path) and datetime.strptime(source_metadata(path)[1], '%Y-%m-%d').date() >= cutoff]
        log('{}: {} raw files'.format(entity, len(files)))
        for csv_path in files:
            metadata = source_metadata(csv_path)
            if not metadata:
                continue
            _, day_iso, hh = metadata
            status, n = convert_one(entity, day_iso, hh, csv_path)
            if status == 'ok':
                total_ok += 1
                total_rows += n
            elif status == 'skip':
                total_skip += 1
            else:
                total_err += 1
                log('  ERROR {} {} {:02d}: {}'.format(entity, day_iso, hh, n))
        log('{}: done'.format(entity))
    log('=== DONE ok={} skip={} err={} rows={:,} in {:.0f}s ==='.format(
        total_ok, total_skip, total_err, total_rows, time.time() - t0))


def one(csv_path):
    """Convert a single file; entity/day/hour derived from its path."""
    metadata = source_metadata(csv_path)
    if not metadata:
        print('badpath', 0, csv_path); return
    entity, day_iso, hh = metadata
    st, n = convert_one(entity, day_iso, hh, csv_path)
    print(st, n, csv_path)


if __name__ == '__main__':
    import sys
    if len(sys.argv) == 3 and sys.argv[1] == '--one':
        one(sys.argv[2])
    elif len(sys.argv) == 3 and sys.argv[1] == '--days':
        main(int(sys.argv[2]))
    else:
        main(30)
