#!/usr/bin/env python3
"""
CDR Direct cache warmer.

Pre-computes and caches the most-frequently-hit filter combos so first
browser open of the day is instant (< 200ms) instead of 20-60 sec cold.

Run order (parallel not needed — API worker is single, sequential is fine):
  1. Yesterday × 4 entities             (highest-EV — main daily-review view)
  2. Today × 4 entities                 (partial-day live view)
  3. Yesterday × per-entity (4 variants)
  4. Last 3 days × 4 entities           (weekly rollup start)

Runs against 127.0.0.1:8090 so it doesn't need external network.
Auth token read from /etc/cdr-direct-token (same as API).

Exit code:
  0 on full success
  1 on any warm failure (but continues through the queue — best-effort)
"""

import json
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

API_URL_BASE = 'http://127.0.0.1:8090'
TOKEN_FILE = '/etc/cdr-direct-token'
ENDPOINTS = (
    '/api/usa-customer-codes',
    '/api/usa-codes',
)
ENTITIES_ALL = ('MyCallConnect', 'SalamTalk', 'Dialphone', 'Vestacall')
PER_ENTITY = tuple((e,) for e in ENTITIES_ALL)

# Per-request timeout — long enough for 4-entity full day cold (~25s post-fix)
# but not silly-long. If cold-compute exceeds this the warm just fails, next
# real user query will still work.
TIMEOUT_SEC = 90


def read_token():
    try:
        with open(TOKEN_FILE) as f:
            return f.read().strip()
    except OSError:
        return ''


def log(*args):
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    print(ts, '[cache-warmer]', *args, flush=True)


def utc_today():
    return datetime.now(timezone.utc).date()


def warm_one(token, endpoint, entities, start_date, end_date, label):
    body = {
        'entities': list(entities),
        'start_date': start_date,
        'end_date': end_date,
        # Empty means all outcomes so ASR uses failures in its denominator.
        'sip_codes': [],
        'customer': '',
        'start_hour': 0,
        'end_hour': 23,
        'sort_by': 'revenue',
        'sort_dir': 'desc',
    }
    payload = json.dumps(body).encode('utf-8')
    req = urllib.request.Request(
        API_URL_BASE + endpoint,
        data=payload,
        headers={
            'Content-Type': 'application/json',
            'X-Auth-Token': token,
        },
        method='POST',
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
            data = resp.read()
            dt = time.time() - t0
            cached = 'unknown'
            try:
                j = json.loads(data)
                c = j.get('_cache') or {}
                cached = 'hit' if c.get('hit') else 'miss'
                pair = j.get('totals', {}).get('pair_count') or j.get('totals', {}).get('code_count', 0)
                log('OK  [{}s] {} {} → {} bytes, cache={}, groups={}'.format(
                    round(dt, 1), endpoint, label, len(data), cached, pair,
                ))
            except Exception:
                log('OK  [{}s] {} {} → {} bytes (unparsable body)'.format(
                    round(dt, 1), endpoint, label, len(data),
                ))
            return True
    except Exception as exc:
        dt = time.time() - t0
        log('ERR [{}s] {} {} → {}'.format(round(dt, 1), endpoint, label, exc))
        return False


def main():
    token = read_token()
    if not token:
        log('FATAL: could not read', TOKEN_FILE)
        return 2

    today = utc_today()
    yesterday = today - timedelta(days=1)
    three_days_ago = today - timedelta(days=2)

    Y = yesterday.isoformat()
    T = today.isoformat()
    L3_start = three_days_ago.isoformat()

    combos = []

    # Priority 1: Yesterday × 4 entities — the #1 daily-review view
    for ep in ENDPOINTS:
        combos.append((ep, ENTITIES_ALL, Y, Y, 'Yesterday × 4ent'))

    # Priority 2: Today × 4 entities — live view during work hours
    for ep in ENDPOINTS:
        combos.append((ep, ENTITIES_ALL, T, T, 'Today × 4ent'))

    # Priority 3: Yesterday × per-entity — entity drill-down
    for ep in ENDPOINTS:
        for e in ENTITIES_ALL:
            combos.append((ep, (e,), Y, Y, 'Yesterday × {}'.format(e)))

    # Priority 4: Last 3 days × 4 entities — trailing view
    for ep in ENDPOINTS:
        combos.append((ep, ENTITIES_ALL, L3_start, T, 'Last 3d × 4ent'))

    log('=== cache warmer start === {} combos queued'.format(len(combos)))
    t_start = time.time()
    ok = err = 0
    for ep, ents, sd, ed, label in combos:
        if warm_one(token, ep, ents, sd, ed, label):
            ok += 1
        else:
            err += 1

    elapsed = time.time() - t_start
    log('=== cache warmer done === ok={} err={} in {:.1f}s ({:.1f}min)'.format(
        ok, err, elapsed, elapsed / 60,
    ))
    return 0 if err == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
