#!/usr/bin/env python3
"""
Prepare yesterday's complete dashboard result before a browser requests it.

In Docker Compose this runs as a small sidecar and calls the API over the
private Compose network. The API owns the persistent SQLite result cache.
"""

import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone


API_URL_BASE = os.environ.get(
    'CDR_API_URL', 'http://127.0.0.1:8090',
).rstrip('/')
TOKEN_FILE = '/etc/cdr-direct-token'
ENDPOINT = '/api/usa-customer-codes'
ENTITIES_ALL = ('MyCallConnect', 'SalamTalk', 'Dialphone', 'Vestacall')
TIMEOUT_SEC = int(os.environ.get('CDR_CACHE_WARM_TIMEOUT', '1800'))
LOOP_INTERVAL_SEC = max(
    60, int(os.environ.get('CDR_CACHE_WARM_INTERVAL', '900')),
)
FINAL_REFRESH_UTC_HOUR = int(
    os.environ.get('CDR_CACHE_FINAL_REFRESH_UTC_HOUR', '2'),
)


def read_token():
    token = os.environ.get('CDR_AUTH_TOKEN', '').strip()
    if token:
        return token
    try:
        with open(TOKEN_FILE, encoding='utf-8') as token_file:
            return token_file.read().strip()
    except OSError:
        return ''


def log(*args):
    stamp = time.strftime('%Y-%m-%d %H:%M:%S')
    print(stamp, '[cache-warmer]', *args, flush=True)


def utc_today():
    return datetime.now(timezone.utc).date()


def build_body(day, force=False):
    """Match static/app.js getQueryBody() exactly for its initial view."""
    body = {
        'start_date': day,
        'end_date': day,
        'entities': list(ENTITIES_ALL),
        'sip_codes': [],
        'customer': None,
        'limit': 5000,
        'sort_by': 'revenue',
        'sort_dir': 'desc',
        'quick_filter': None,
    }
    if force:
        body['force_refresh'] = True
    return body


def warm_one(token, day, force=False):
    payload = json.dumps(build_body(day, force=force)).encode('utf-8')
    request = urllib.request.Request(
        API_URL_BASE + ENDPOINT,
        data=payload,
        headers={
            'Content-Type': 'application/json',
            'X-Auth-Token': token,
        },
        method='POST',
    )
    started = time.time()
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SEC) as response:
            data = response.read()
        elapsed = time.time() - started
        parsed = json.loads(data)
        cache_meta = parsed.get('_cache') or {}
        cache_state = 'hit' if cache_meta.get('hit') else 'computed'
        group_count = (
            parsed.get('totals', {}).get('pair_count')
            or parsed.get('totals', {}).get('code_count', 0)
        )
        log(
            'ready day={} elapsed={:.1f}s cache={} groups={} force={}'.format(
                day, elapsed, cache_state, group_count, force,
            )
        )
        return True
    except Exception as exc:
        elapsed = time.time() - started
        log('failed day={} elapsed={:.1f}s error={}'.format(day, elapsed, exc))
        return False


def warm_yesterday(token, force=False):
    yesterday = (utc_today() - timedelta(days=1)).isoformat()
    return warm_one(token, yesterday, force=force)


def run_loop(token):
    last_final_refresh_day = None
    while True:
        now = datetime.now(timezone.utc)
        after_finalization = now.hour >= FINAL_REFRESH_UTC_HOUR
        days_back = 1 if after_finalization else 2
        snapshot_day = (now.date() - timedelta(days=days_back)).isoformat()
        force = after_finalization and last_final_refresh_day != snapshot_day
        ok = warm_one(token, snapshot_day, force=force)
        if force and ok:
            last_final_refresh_day = snapshot_day
        time.sleep(LOOP_INTERVAL_SEC)


def main():
    token = read_token()
    if not token:
        log('fatal: set CDR_AUTH_TOKEN or provide {}'.format(TOKEN_FILE))
        return 2

    if '--loop' in sys.argv:
        log(
            'loop started API={} interval={}s final_refresh={:02d}:00UTC'.format(
                API_URL_BASE, LOOP_INTERVAL_SEC, FINAL_REFRESH_UTC_HOUR,
            )
        )
        run_loop(token)
        return 0
    return 0 if warm_yesterday(token) else 1


if __name__ == '__main__':
    sys.exit(main())
