#!/usr/bin/env python3
"""
One-shot backfill — converts last N days of CSV.gz files into the DuckDB native DB.
Idempotent: skips files already in ingest_log.

Usage:  python3 backfill.py [days=4]
"""
import sys
import os
import time
import glob
import re
from datetime import datetime, timedelta

sys.path.insert(0, '/opt/cdr-direct')
import db
from settings import CDR_ROOT

ENTITIES = ('MyCallConnect', 'SalamTalk', 'Dialphone', 'Vestacall')
DEFAULT_DAYS = 4


def find_files(days):
    """Walk CDR_ROOT, yield (entity, file_hour_iso, csv_path) for the last N days."""
    today = datetime.utcnow().date()
    out = []
    for days_back in range(days):
        d = today - timedelta(days=days_back)
        for entity in ENTITIES:
            pattern = '{}/{}/{:04d}/{:02d}/{:02d}/*.csv.gz'.format(
                CDR_ROOT, entity, d.year, d.month, d.day
            )
            for csv in sorted(glob.glob(pattern)):
                m = re.match(r'^(\d{2})\.csv\.gz$', os.path.basename(csv))
                if not m:
                    continue
                hour = int(m.group(1))
                file_hour = datetime(d.year, d.month, d.day, hour, 0, 0).isoformat(' ')
                out.append((entity, file_hour, csv))
    return out


def log(*args):
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    print(ts, '[backfill]', *args, flush=True)


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DAYS
    log('initializing DuckDB schema…')
    db.init()
    log('schema OK. scanning last {} day(s) for CSV files…'.format(days))

    files = find_files(days)
    log('found {} files'.format(len(files)))

    skipped = ingested = errored = 0
    t_start = time.time()

    for i, (entity, file_hour, csv_path) in enumerate(files, 1):
        if db.already_ingested(entity, file_hour, csv_path):
            skipped += 1
            continue
        t0 = time.time()
        n_rows, err = db.ingest_csv_file(csv_path, entity, file_hour)
        dt = time.time() - t0
        if err:
            log('[{}/{}] ERROR {} {} → {}'.format(i, len(files), entity, file_hour, err[:200]))
            errored += 1
        else:
            log('[{}/{}] {} {} ⇒ {:,} rows in {:.1f}s'.format(
                i, len(files), entity, file_hour, n_rows, dt
            ))
            ingested += 1

    elapsed = time.time() - t_start
    log('=== DONE ===')
    log('ingested={} skipped={} errored={} in {:.1f}s ({:.1f} min)'.format(
        ingested, skipped, errored, elapsed, elapsed / 60
    ))

    s = db.stats()
    log('db state: rows={total_rows} files={files_ingested} oldest={oldest_hour} newest={newest_hour} size={size_mb:.1f}MB'.format(
        size_mb=s.get('db_size_bytes', 0) / (1024 * 1024), **s
    ))
    return 0


if __name__ == '__main__':
    sys.exit(main())
