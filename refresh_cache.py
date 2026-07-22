#!/usr/bin/env python3
"""
Background cache refresher — runs every 5 min via systemd timer.

Maintains a rolling 4-day window of hot-cached query results.
Each run:
  1. Generate hot list (4 days × 5 entity combos × 2 views = 40 entries, + rollups)
  2. For each entry, refresh if past tier TTL
  3. Daily roll: drop cache entries older than 4 days
"""
import sys
import os
import time
import json
from datetime import date, timedelta

# Allow importing api + cache modules
sys.path.insert(0, '/opt/cdr-direct')
import cache
from api import compute_usa_codes, compute_usa_customer_codes, ENTITIES

# Hot entity combos: each entity solo + all 4 combined
HOT_ENTITY_COMBOS = [
    ['MyCallConnect'],
    ['SalamTalk'],
    ['Dialphone'],
    ['Vestacall'],
    list(ENTITIES),  # all 4
]

LOCK_PATH = '/tmp/cdr-direct-refresher.lock'
LOG_PREFIX = '[refresher]'


def log(*args):
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    print(ts, LOG_PREFIX, *args, flush=True)


def build_hot_list():
    """Generate (endpoint, body) tuples for the rolling 4-day hot window."""
    today = date.today()
    out = []

    # Single-day entries: today, yesterday, 2-days-ago, 3-days-ago
    for days_back in range(4):
        d = (today - timedelta(days=days_back)).isoformat()
        for entities in HOT_ENTITY_COMBOS:
            for endpoint in ('usa-codes', 'usa-customer-codes'):
                out.append((endpoint, {
                    'start_date': d,
                    'end_date': d,
                    'entities': entities,
                    'sip_codes': [],
                    'sort_by': 'revenue',
                    'sort_dir': 'desc',
                }))

    # Multi-day rollups: Last 3 days, Last 7 days (only for all-4-combined)
    for span, label in [(3, 'last3'), (7, 'last7')]:
        sd = (today - timedelta(days=span - 1)).isoformat()
        ed = today.isoformat()
        for entities in HOT_ENTITY_COMBOS:
            for endpoint in ('usa-codes', 'usa-customer-codes'):
                out.append((endpoint, {
                    'start_date': sd,
                    'end_date': ed,
                    'entities': entities,
                    'sip_codes': [],
                    'sort_by': 'revenue',
                    'sort_dir': 'desc',
                }))

    return out


def refresh_entry(endpoint, body):
    """Refresh this entry if stale. Returns ('refreshed'|'fresh'|'error', detail)."""
    cache_key = cache.make_key(endpoint, body)
    tier = cache.classify_tier(body)
    cached = cache.get(cache_key)

    if cached and not cached['stale']:
        return ('fresh', tier)

    compute_fn = compute_usa_codes if endpoint == 'usa-codes' else compute_usa_customer_codes

    t0 = time.time()
    result = compute_fn(body)
    compute_ms = int((time.time() - t0) * 1000)

    if 'error' in result:
        return ('error', result.get('error', 'unknown'))

    cache.put(cache_key, endpoint, body, result, tier, compute_ms)
    return ('refreshed', '{} {}ms'.format(tier, compute_ms))


def acquire_lock():
    """Stale-lock detection — release if older than 30 min (refresher hung)."""
    if os.path.exists(LOCK_PATH):
        age = time.time() - os.path.getmtime(LOCK_PATH)
        if age < 1800:
            log('skip — lock held by another run, age {:.0f}s'.format(age))
            return False
        log('stale lock found ({:.0f}s old), removing'.format(age))
        os.remove(LOCK_PATH)
    with open(LOCK_PATH, 'w') as f:
        f.write(str(os.getpid()))
    return True


def release_lock():
    try:
        os.remove(LOCK_PATH)
    except OSError:
        pass


def daily_roll():
    """Drop cache entries with end_date older than 4 days ago."""
    cutoff = (date.today() - timedelta(days=4)).isoformat()
    dropped = cache.drop_older_than(cutoff)
    if dropped > 0:
        log('rolled window: dropped {} entries with end_date < {}'.format(dropped, cutoff))
    return dropped


CYCLE_BUDGET_SEC = 2700  # 45 min max per cycle — leaves headroom under systemd 50-min kill


def main():
    if not acquire_lock():
        return 0

    try:
        t_start = time.time()
        log('=== refresh cycle start ===')

        hot_list = build_hot_list()
        log('hot list size: {} entries (budget {}s)'.format(len(hot_list), CYCLE_BUDGET_SEC))

        stats = {'fresh': 0, 'refreshed': 0, 'error': 0, 'budget-exceeded': 0}
        for endpoint, body in hot_list:
            # Self-exit before systemd kills us
            elapsed = time.time() - t_start
            if elapsed > CYCLE_BUDGET_SEC:
                stats['budget-exceeded'] += 1
                continue

            try:
                status, detail = refresh_entry(endpoint, body)
                stats[status] = stats.get(status, 0) + 1
                if status == 'refreshed':
                    log('  refreshed: {} {} ents={} ({})'.format(
                        endpoint, body['start_date'],
                        ','.join(body['entities'][:2]) + ('+' if len(body['entities']) > 2 else ''),
                        detail
                    ))
                elif status == 'error':
                    log('  ERROR: {} {} → {}'.format(endpoint, body['start_date'], detail))
            except Exception as e:
                stats['error'] = stats.get('error', 0) + 1
                log('  EXCEPTION: {} {} → {}'.format(endpoint, body['start_date'], e))

        elapsed = time.time() - t_start
        log('cycle done: fresh={fresh} refreshed={refreshed} error={error} budget-exceeded={budget-exceeded} in {elapsed:.1f}s'.format(
            elapsed=elapsed, **stats
        ))

        # Daily roll (cheap — just runs SQL DELETE)
        daily_roll()

        # Cache stats summary
        cs = cache.stats()
        log('cache stats: total={total_entries} db_size={size_mb:.1f}MB by_tier={by_tier}'.format(
            size_mb=cs['db_size_bytes'] / (1024 * 1024),
            **cs
        ))
    finally:
        release_lock()

    return 0


if __name__ == '__main__':
    sys.exit(main())
