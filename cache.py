"""
SQLite cache for cdr-direct query results.
Stale-while-revalidate semantics: always returns cached if it exists,
flags `stale=true` if past TTL. Background refresher (refresh_cache.py)
keeps the rolling 4-day window pre-warmed.
"""
import sqlite3
import json
import time
import os
from datetime import date, datetime, timedelta, timezone

from settings import CACHE_DB, MAX_RESULT_ROWS

# Bump whenever the shape or meaning of report rows changes. Version 3 makes
# `code` the exact origin billed prefix and adds the independent termination
# billed prefix as `term_code`, so cached destination-derived rows are invalid.
# Version 4: customer x state now derives `state` from the billed-prefix NPA
# instead of the ANI/raw switch column, so cached ANI-derived rows are invalid.
CACHE_KEY_VERSION = 4
DAILY_SNAPSHOT_ENTITIES = (
    'MyCallConnect', 'SalamTalk', 'Dialphone', 'Vestacall',
)
try:
    DAILY_SNAPSHOT_FINAL_UTC_HOUR = max(
        0, min(23, int(os.environ.get('CDR_CACHE_FINAL_REFRESH_UTC_HOUR', '2'))),
    )
except ValueError:
    DAILY_SNAPSHOT_FINAL_UTC_HOUR = 2

# Tier → TTL in seconds. Tier classification by date-class of the query.
TIER_TTLS = {
    'today':      300,     # 5 min  — live data
    'yesterday':  900,     # 15 min — late files may trickle in
    'recent':     3600,    # 1 hour — 2-3 days old, stable
    'historical': 86400,   # 24 hours — older single day or any multi-day range
}


def init():
    """Create cache table if not exists. Idempotent."""
    os.makedirs(os.path.dirname(CACHE_DB), exist_ok=True)
    conn = sqlite3.connect(CACHE_DB)
    try:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS cache (
                cache_key TEXT PRIMARY KEY,
                endpoint TEXT NOT NULL,
                start_date TEXT,
                end_date TEXT,
                response_json TEXT NOT NULL,
                refreshed_at REAL NOT NULL,
                tier TEXT NOT NULL,
                compute_ms INTEGER
            )
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_end_date ON cache(end_date)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_refreshed_at ON cache(refreshed_at)')
        conn.commit()
    finally:
        conn.close()


def make_key(endpoint, body):
    """
    Build canonical cache key from endpoint + request body.
    Sort lists so [MCC,ST] and [ST,MCC] hit the same key.
    """
    canon = {
        'version': CACHE_KEY_VERSION,
        'endpoint': endpoint,
        'start_date': body.get('start_date'),
        'end_date': body.get('end_date') or body.get('start_date'),
        'entities': sorted(body.get('entities') or []),
        'sip_codes': sorted(body.get('sip_codes') or []),
        'reasons': sorted(str(r).upper() for r in (body.get('reasons') or [])),
        'customer': body.get('customer'),
        'start_hour': body.get('start_hour'),
        'end_hour': body.get('end_hour'),
        'limit': body.get('limit', MAX_RESULT_ROWS),
        'sort_by': body.get('sort_by') or 'revenue',
        'sort_dir': body.get('sort_dir') or 'desc',
        'quick_filter': body.get('quick_filter'),
    }
    return json.dumps(canon, sort_keys=True, separators=(',', ':'))


def classify_tier(body):
    """
    Return tier label based on date-class of the query.
    Multi-day ranges and queries with explicit customer filter → 'historical'
    (less hot, longer TTL).
    """
    today = date.today()
    yesterday = today - timedelta(days=1)
    sd_str = body.get('start_date')
    ed_str = body.get('end_date') or sd_str
    if not sd_str:
        return 'historical'

    try:
        sd = datetime.strptime(sd_str, '%Y-%m-%d').date()
        ed = datetime.strptime(ed_str, '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return 'historical'

    # Multi-day range
    if sd != ed:
        return 'historical'
    # Single-day range
    if sd == today:
        return 'today'
    if sd == yesterday:
        return 'yesterday'
    if (today - sd).days <= 3:
        return 'recent'
    return 'historical'


def get(cache_key):
    """Return dict with cached data + metadata, or None if miss."""
    conn = sqlite3.connect(CACHE_DB)
    try:
        row = conn.execute(
            'SELECT response_json, refreshed_at, tier, compute_ms FROM cache WHERE cache_key=?',
            (cache_key,)
        ).fetchone()
    finally:
        conn.close()

    if not row:
        return None

    resp_json, refreshed_at, tier, compute_ms = row
    age = time.time() - refreshed_at
    ttl = TIER_TTLS.get(tier, 86400)
    return {
        'response': json.loads(resp_json),
        'refreshed_at': refreshed_at,
        'age_seconds': age,
        'tier': tier,
        'stale': age > ttl,
        'ttl': ttl,
        'compute_ms': compute_ms,
    }


def put(cache_key, endpoint, body, response, tier, compute_ms=0):
    """Store/replace cache entry."""
    conn = sqlite3.connect(CACHE_DB)
    try:
        conn.execute(
            'REPLACE INTO cache (cache_key, endpoint, start_date, end_date, response_json, refreshed_at, tier, compute_ms) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (
                cache_key, endpoint,
                body.get('start_date'),
                body.get('end_date') or body.get('start_date'),
                json.dumps(response),
                time.time(),
                tier,
                compute_ms,
            )
        )
        conn.commit()
    finally:
        conn.close()


def daily_snapshot_body(day):
    """Canonical request used by both the browser and daily cache warmer."""
    day_str = day.isoformat() if hasattr(day, 'isoformat') else str(day)
    return {
        'start_date': day_str,
        'end_date': day_str,
        'entities': list(DAILY_SNAPSHOT_ENTITIES),
        'sip_codes': [],
        'customer': None,
        'limit': MAX_RESULT_ROWS,
        'sort_by': 'revenue',
        'sort_dir': 'desc',
        'quick_filter': None,
    }


def latest_daily_snapshot(
        endpoint='usa-customer-codes', lookback_days=7, today=None,
        final_utc_hour=None):
    """
    Return the newest prepared full-day dashboard response without computing.

    Yesterday is preferred. Falling back through the previous week means a
    browser still receives a complete day immediately while the next snapshot
    is being prepared.
    """
    if today is None:
        today = datetime.now(timezone.utc).date()
    if final_utc_hour is None:
        final_utc_hour = DAILY_SNAPSHOT_FINAL_UTC_HOUR
    for days_back in range(1, max(1, int(lookback_days)) + 1):
        snapshot_date = today - timedelta(days=days_back)
        body = daily_snapshot_body(snapshot_date)
        cached = get(make_key(endpoint, body))
        next_day = snapshot_date + timedelta(days=1)
        finalized_after = datetime(
            next_day.year, next_day.month, next_day.day,
            int(final_utc_hour), tzinfo=timezone.utc,
        ).timestamp()
        # A result computed while hourly files were still arriving is not a
        # complete-day snapshot. Keep showing the previous completed day until
        # the post-ingestion refresh has finished.
        if cached and cached['refreshed_at'] >= finalized_after:
            cached['snapshot_date'] = snapshot_date.isoformat()
            return cached
    return None


def drop_older_than(cutoff_date_str):
    """Delete cache entries with end_date < cutoff (used by daily roll)."""
    conn = sqlite3.connect(CACHE_DB)
    try:
        deleted = conn.execute(
            'DELETE FROM cache WHERE end_date < ?',
            (cutoff_date_str,)
        ).rowcount
        conn.commit()
        return deleted
    finally:
        conn.close()


def stats():
    """Return cache size + tier counts for monitoring."""
    conn = sqlite3.connect(CACHE_DB)
    try:
        total = conn.execute('SELECT COUNT(*) FROM cache').fetchone()[0]
        by_tier = dict(conn.execute('SELECT tier, COUNT(*) FROM cache GROUP BY tier').fetchall())
        oldest = conn.execute('SELECT MIN(end_date) FROM cache').fetchone()[0]
        newest = conn.execute('SELECT MAX(end_date) FROM cache').fetchone()[0]
        db_size = os.path.getsize(CACHE_DB) if os.path.exists(CACHE_DB) else 0
    finally:
        conn.close()
    return {
        'total_entries': total,
        'by_tier': by_tier,
        'oldest_end_date': oldest,
        'newest_end_date': newest,
        'db_size_bytes': db_size,
    }


# Auto-init on import
init()
