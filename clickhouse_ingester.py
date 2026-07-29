#!/usr/bin/env python3
"""Incrementally load stable 46Labs hourly CSV.gz files into ClickHouse."""

import argparse
import csv
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


ENTITIES = ('MyCallConnect', 'SalamTalk', 'Dialphone', 'Vestacall')
HOUR_FILE_RE = re.compile(r'^(?P<hour>[01]\d|2[0-3])\.csv\.gz$')
HEADER_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
REQUIRED_HEADERS = {
    'sip_code', 'call_start_time', 'call_end_time', 'term_media_ip',
}

CDR_ROOT = Path(os.environ.get('CDR_ROOT', '/data/raw'))
CLICKHOUSE_URL = os.environ.get(
    'CLICKHOUSE_URL', 'http://clickhouse:8123',
).rstrip('/')
CLICKHOUSE_DATABASE = os.environ.get('CLICKHOUSE_DATABASE', 'cdr')
CLICKHOUSE_USER = os.environ.get('CLICKHOUSE_USER', 'cdr_admin')
CLICKHOUSE_PASSWORD = os.environ.get('CLICKHOUSE_PASSWORD', '')
LOOKBACK_DAYS = max(1, int(os.environ.get('CDR_INGEST_LOOKBACK_DAYS', '2')))
MIN_AGE_SECONDS = max(
    60, int(os.environ.get('CDR_INGEST_MIN_AGE_SECONDS', '600')),
)
LOOP_INTERVAL_SECONDS = max(
    30, int(os.environ.get('CDR_INGEST_INTERVAL_SECONDS', '60')),
)
FILE_PAUSE_SECONDS = max(
    0.0, float(os.environ.get('CDR_INGEST_FILE_PAUSE_SECONDS', '1')),
)
QUERY_TIMEOUT_SECONDS = max(
    60, int(os.environ.get('CDR_INGEST_QUERY_TIMEOUT', '1800')),
)


@dataclass(frozen=True)
class SourceFile:
    entity: str
    file_hour: datetime
    path: Path
    clickhouse_path: str
    size: int
    mtime_ns: int


def log(*parts):
    stamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')
    print(stamp, '[clickhouse-ingester]', *parts, flush=True)


def sql_string(value):
    value = str(value)
    return "'" + value.replace('\\', '\\\\').replace("'", "\\'") + "'"


def sql_identifier(value):
    if not HEADER_RE.fullmatch(value):
        raise ValueError('unsafe CSV header: {!r}'.format(value))
    return '`{}`'.format(value)


class ClickHouseHTTP:
    def __init__(
            self, url=CLICKHOUSE_URL, database=CLICKHOUSE_DATABASE,
            user=CLICKHOUSE_USER, password=CLICKHOUSE_PASSWORD,
            timeout=QUERY_TIMEOUT_SECONDS):
        self.url = url.rstrip('/')
        self.database = database
        self.user = user
        self.password = password
        self.timeout = timeout

    def execute(self, query):
        url = self.url + '/?' + urllib.parse.urlencode({
            'database': self.database,
            'wait_end_of_query': '1',
        })
        request = urllib.request.Request(
            url,
            data=query.encode('utf-8'),
            headers={
                'Content-Type': 'text/plain; charset=utf-8',
                'X-ClickHouse-User': self.user,
                'X-ClickHouse-Key': self.password,
            },
            method='POST',
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read().decode('utf-8', errors='replace')
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode('utf-8', errors='replace')
            raise RuntimeError(
                'ClickHouse HTTP {}: {}'.format(exc.code, detail[:1000]),
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeError('ClickHouse unavailable: {}'.format(exc)) from exc

    def json_rows(self, query):
        text = self.execute(query.rstrip().rstrip(';') + ' FORMAT JSONEachRow')
        return [json.loads(line) for line in text.splitlines() if line.strip()]


def read_csv_header(path):
    with gzip.open(path, mode='rt', encoding='utf-8-sig', newline='') as stream:
        row = next(csv.reader(stream), None)
    if not row:
        raise ValueError('CSV has no header row')
    headers = [name.strip() for name in row]
    if len(set(headers)) != len(headers):
        raise ValueError('CSV contains duplicate header names')
    for name in headers:
        sql_identifier(name)
    missing = sorted(REQUIRED_HEADERS.difference(headers))
    if missing:
        raise ValueError(
            'required 46Labs fields missing: {}'.format(', '.join(missing)),
        )
    return headers


def discover_files(root=CDR_ROOT, lookback_days=LOOKBACK_DAYS, now=None):
    now = now or datetime.now(timezone.utc)
    today = now.date()
    found = []
    for days_back in reversed(range(lookback_days)):
        day = today - timedelta(days=days_back)
        for entity in ENTITIES:
            day_dir = root / entity / '{:04d}'.format(day.year) / \
                '{:02d}'.format(day.month) / '{:02d}'.format(day.day)
            if not day_dir.is_dir():
                continue
            for path in sorted(day_dir.glob('*.csv.gz')):
                match = HOUR_FILE_RE.fullmatch(path.name)
                if not match:
                    continue
                stat = path.stat()
                age = now.timestamp() - stat.st_mtime
                if age < MIN_AGE_SECONDS:
                    continue
                hour = int(match.group('hour'))
                file_hour = datetime(
                    day.year, day.month, day.day, hour, tzinfo=timezone.utc,
                )
                relative = 'cdr/{}/{:04d}/{:02d}/{:02d}/{}'.format(
                    entity, day.year, day.month, day.day, path.name,
                )
                found.append(SourceFile(
                    entity=entity,
                    file_hour=file_hour,
                    path=path,
                    clickhouse_path=relative,
                    size=stat.st_size,
                    mtime_ns=stat.st_mtime_ns,
                ))
    return found


def source_text(headers, name):
    return sql_identifier(name) if name in headers else "''"


def source_datetime(headers, name):
    return "parseDateTimeBestEffortOrNull(nullIf({}, ''), 'UTC')".format(
        source_text(headers, name),
    )


def source_uint(headers, name, bits):
    return 'toUInt{}OrZero({})'.format(bits, source_text(headers, name))


def source_float(headers, name, bits=64):
    return 'toFloat{}OrZero({})'.format(bits, source_text(headers, name))


def build_insert_sql(source, headers):
    entity = sql_string(source.entity)
    file_hour = sql_string(source.file_hour.strftime('%Y-%m-%d %H:%M:%S'))
    file_path = sql_string(str(source.path))
    clickhouse_path = sql_string(source.clickhouse_path)
    structure = sql_string(', '.join(
        '{} String'.format(name) for name in headers
    ))

    start_time = source_datetime(headers, 'call_start_time')
    answer_time = source_datetime(headers, 'call_answered_time')
    end_time = source_datetime(headers, 'call_end_time')
    event_time = "coalesce({}, {}, toDateTime({}, 'UTC'))".format(
        end_time, start_time, file_hour,
    )
    record_key = (
        'cityHash64({}, {}, {}, {}, {}, {}, {}, {})'.format(
            entity,
            file_hour,
            source_text(headers, 'cdr_uuid'),
            source_text(headers, 'callid'),
            source_text(headers, 'from_did'),
            source_text(headers, 'to_did'),
            source_text(headers, 'call_start_time'),
            source_text(headers, 'attempt_counter'),
        )
    )

    columns = [
        'record_key', 'entity', 'file_hour', 'file_path', 'source_size',
        'source_mtime_ns', 'event_time',
        'cdr_uuid', 'callid', 'call_start_time', 'call_answered_time',
        'call_end_time', 'from_did', 'to_did', 'lrn_did',
        'sip_code', 'sip_reason', 'reason', 'duration_real', 'pdd',
        'attempt_counter', 'terminator', 'leg', 'direction',
        'orig_carrier_name', 'orig_trunk_group_name', 'orig_ip',
        'orig_media_ip', 'orig_juris', 'orig_rate',
        'orig_billed_duration', 'orig_cost',
        'term_carrier_name', 'term_trunk_group_name', 'term_ip',
        'term_media_ip', 'term_juris', 'term_rate',
        'term_billed_duration', 'term_cost', 'term_state',
        'term_ratecenter', 'term_destination',
        'stir_x5u', 'stir_attest', 'mos_min', 'mos_avg', 'mos_max',
    ]
    expressions = [
        record_key, entity, "toDateTime({}, 'UTC')".format(file_hour),
        file_path, str(source.size), str(source.mtime_ns), event_time,
        source_text(headers, 'cdr_uuid'),
        source_text(headers, 'callid'),
        start_time, answer_time, end_time,
        source_text(headers, 'from_did'),
        source_text(headers, 'to_did'),
        source_text(headers, 'lrn_did'),
        source_uint(headers, 'sip_code', 16),
        source_text(headers, 'sip_reason'),
        source_text(headers, 'reason'),
        source_uint(headers, 'duration_real', 32),
        source_uint(headers, 'pdd', 32),
        source_uint(headers, 'attempt_counter', 16),
        source_text(headers, 'terminator'),
        source_text(headers, 'leg'),
        source_text(headers, 'direction'),
        source_text(headers, 'orig_carrier_name'),
        source_text(headers, 'orig_trunk_group_name'),
        source_text(headers, 'orig_ip'),
        source_text(headers, 'orig_media_ip'),
        source_text(headers, 'orig_juris'),
        source_float(headers, 'orig_rate'),
        source_uint(headers, 'orig_billed_duration', 32),
        source_float(headers, 'orig_cost'),
        source_text(headers, 'term_carrier_name'),
        source_text(headers, 'term_trunk_group_name'),
        source_text(headers, 'term_ip'),
        source_text(headers, 'term_media_ip'),
        source_text(headers, 'term_juris'),
        source_float(headers, 'term_rate'),
        source_uint(headers, 'term_billed_duration', 32),
        source_float(headers, 'term_cost'),
        source_text(headers, 'term_state'),
        source_text(headers, 'term_ratecenter'),
        source_text(headers, 'term_destination'),
        source_text(headers, 'stir_x5u'),
        source_text(headers, 'stir_attest'),
        source_float(headers, 'mos_min', 32),
        source_float(headers, 'mos_avg', 32),
        source_float(headers, 'mos_max', 32),
    ]
    return """
INSERT INTO cdr.raw_cdr ({columns})
SELECT
    {expressions}
FROM file({path}, 'CSVWithNames', {structure})
SETTINGS
    input_format_csv_empty_as_default = 1,
    input_format_with_names_use_header = 1
""".format(
        columns=', '.join(columns),
        expressions=',\n    '.join(expressions),
        path=clickhouse_path,
        structure=structure,
    ).strip()


def latest_log(client, source):
    rows = client.json_rows("""
SELECT
    source_size,
    source_mtime_ns,
    rows_ingested,
    status,
    message
FROM cdr.ingest_log FINAL
WHERE file_path = {path}
LIMIT 1
""".format(path=sql_string(str(source.path))))
    return rows[0] if rows else None


def rows_for_source(client, source):
    rows = client.json_rows("""
SELECT count() AS rows
FROM cdr.raw_cdr
WHERE entity = {entity}
  AND file_hour = toDateTime({file_hour}, 'UTC')
""".format(
        entity=sql_string(source.entity),
        file_hour=sql_string(source.file_hour.strftime('%Y-%m-%d %H:%M:%S')),
    ))
    return int(rows[0]['rows']) if rows else 0


def write_log(client, source, status, rows=0, message=''):
    client.execute("""
INSERT INTO cdr.ingest_log
(
    file_path, entity, file_hour, source_size, source_mtime_ns,
    rows_ingested, status, message
)
VALUES
(
    {path}, {entity}, toDateTime({file_hour}, 'UTC'), {size}, {mtime},
    {rows}, {status}, {message}
)
""".format(
        path=sql_string(str(source.path)),
        entity=sql_string(source.entity),
        file_hour=sql_string(source.file_hour.strftime('%Y-%m-%d %H:%M:%S')),
        size=source.size,
        mtime=source.mtime_ns,
        rows=max(0, int(rows)),
        status=sql_string(status),
        message=sql_string(str(message)[:1000]),
    ))


def same_fingerprint(log_row, source):
    return (
        int(log_row.get('source_size') or 0) == source.size
        and int(log_row.get('source_mtime_ns') or 0) == source.mtime_ns
    )


def ingest_one(client, source):
    previous = latest_log(client, source)
    if previous:
        status = previous.get('status')
        fingerprint_matches = same_fingerprint(previous, source)
        previous_rows = int(previous.get('rows_ingested') or 0)

        if status in {'done', 'changed'}:
            if fingerprint_matches:
                return 'skipped', previous_rows
            message = (
                'source changed after ingestion; automatic reinsert blocked '
                'to protect materialized-view totals'
            )
            write_log(client, source, 'changed', previous_rows, message)
            log('CHANGED', source.path, '-', message)
            return 'changed', previous_rows

        if status in {'ingesting', 'error'}:
            recovered_rows = rows_for_source(client, source)
            if recovered_rows:
                recovered_status = 'done' if fingerprint_matches else 'changed'
                message = (
                    'recovered completed insert after ingester restart'
                    if fingerprint_matches
                    else 'insert completed before source changed; rebuild required'
                )
                write_log(
                    client, source, recovered_status, recovered_rows, message,
                )
                return (
                    ('recovered', recovered_rows)
                    if fingerprint_matches
                    else ('changed', recovered_rows)
                )

    try:
        headers = read_csv_header(source.path)
        write_log(client, source, 'ingesting', 0, 'validated; insert started')
        client.execute(build_insert_sql(source, headers))
        inserted_rows = rows_for_source(client, source)
        write_log(client, source, 'done', inserted_rows, 'complete')
        return 'ingested', inserted_rows
    except Exception as exc:
        try:
            write_log(client, source, 'error', 0, str(exc))
        except Exception as log_exc:
            log('could not write error status:', log_exc)
        raise


def run_once(client, lookback_days):
    sources = discover_files(lookback_days=lookback_days)
    stats = {
        'ingested': 0, 'recovered': 0, 'skipped': 0,
        'changed': 0, 'error': 0, 'rows': 0,
    }
    log('scan found {} stable hourly files'.format(len(sources)))
    for source in sources:
        try:
            status, rows = ingest_one(client, source)
            stats[status] += 1
            if status in {'ingested', 'recovered'}:
                stats['rows'] += rows
                log(
                    status.upper(), source.entity,
                    source.file_hour.isoformat(), '{:,} rows'.format(rows),
                )
        except Exception as exc:
            stats['error'] += 1
            log('ERROR', source.path, '-', str(exc)[:500])
        if FILE_PAUSE_SECONDS:
            time.sleep(FILE_PAUSE_SECONDS)
    log('cycle complete', json.dumps(stats, sort_keys=True))
    return 0 if stats['error'] == 0 else 1


def wait_for_clickhouse(client):
    while True:
        try:
            client.execute('SELECT 1')
            log('ClickHouse is ready at', client.url)
            return
        except Exception as exc:
            log('waiting for ClickHouse:', str(exc)[:200])
            time.sleep(10)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--loop', action='store_true')
    parser.add_argument('--backfill-days', type=int, default=LOOKBACK_DAYS)
    args = parser.parse_args(argv)

    if not CLICKHOUSE_PASSWORD:
        log('fatal: CLICKHOUSE_PASSWORD is required')
        return 2
    client = ClickHouseHTTP()
    wait_for_clickhouse(client)
    lookback_days = max(1, args.backfill_days)

    if args.once and not args.loop:
        return run_once(client, lookback_days)
    while True:
        run_once(client, lookback_days)
        time.sleep(LOOP_INTERVAL_SECONDS)


if __name__ == '__main__':
    sys.exit(main())
