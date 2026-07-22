#!/usr/bin/env python3
"""
Nightly append job — runs daily at 01:00 UTC via systemd timer.

Picks up any new hourly CSV.gz files not yet in ingest_log,
appends them to cdr_records, then rolls out files older than 4 days.
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
RETENTION_DAYS = 4
LOOKBACK_DAYS = 4   # match retention — anything older is dropped at end anyway
LOCK_PATH = '/tmp/cdr-direct-nightly.lock'


def log(*args):
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    print(ts, '[nightly]', *args, flush=True)


def find_new_files():
    """Find all CSV files within LOOKBACK window that aren't yet in ingest_log."""
    today = datetime.utcnow().date()
    out = []
    for days_back in range(LOOKBACK_DAYS):
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
                # Never ingest a file that 46Labs may still be writing.
                if time.time() - os.path.getmtime(csv) < 120:
                    continue
                if not db.already_ingested(entity, file_hour, csv):
                    out.append((entity, file_hour, csv))
    return out


def acquire_lock():
    if os.path.exists(LOCK_PATH):
        age = time.time() - os.path.getmtime(LOCK_PATH)
        if age < 7200:
            log('skip — lock held, age {:.0f}s'.format(age))
            return False
        log('stale lock ({:.0f}s old), removing'.format(age))
        os.remove(LOCK_PATH)
    with open(LOCK_PATH, 'w') as f:
        f.write(str(os.getpid()))
    return True


def release_lock():
    try:
        os.remove(LOCK_PATH)
    except OSError:
        pass


def main():
    if not acquire_lock():
        return 0

    try:
        t_start = time.time()
        log('=== nightly append start ===')

        new_files = find_new_files()
        log('found {} new files to ingest'.format(len(new_files)))

        ingested = errored = 0
        for entity, file_hour, csv_path in new_files:
            t0 = time.time()
            n, err = db.ingest_csv_file(csv_path, entity, file_hour)
            if err:
                log('  ERROR {} {} → {}'.format(entity, file_hour, err[:200]))
                errored += 1
            else:
                log('  {} {} ⇒ {:,} rows in {:.1f}s'.format(entity, file_hour, n, time.time() - t0))
                ingested += 1

        # Daily roll: drop rows older than RETENTION_DAYS
        cutoff_date = (datetime.utcnow().date() - timedelta(days=RETENTION_DAYS)).isoformat()
        cutoff_iso = cutoff_date + ' 00:00:00'
        log('rolling window: dropping rows with file_hour < {}'.format(cutoff_iso))
        ok = db.drop_older_than(cutoff_iso)
        log('roll {}'.format('OK' if ok else 'FAILED'))

        elapsed = time.time() - t_start
        log('=== done: ingested={} errored={} in {:.1f}s ==='.format(ingested, errored, elapsed))

        s = db.stats()
        log('db: rows={total_rows:,} files={files_ingested} size={size_mb:.1f}MB'.format(
            size_mb=s.get('db_size_bytes', 0) / (1024 * 1024), **s
        ))
    finally:
        release_lock()

    return 0


if __name__ == '__main__':
    sys.exit(main())
