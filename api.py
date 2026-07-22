"""
CDR Direct - Read-only query API + web UI over local 46labs CSV files via DuckDB.

Endpoints:
  GET  /health             - liveness probe (no auth)
  POST /sql                - raw DuckDB query, returns array of rows (X-Auth-Token)
  GET  /ui                 - HTML dashboard (no auth on page; JS calls auth'd APIs)
  GET  /static/<file>      - JS/CSS assets
  POST /api/usa-codes      - per-USA-code aggregate (auth'd, used by UI, cached)
  POST /api/usa-customer-codes - per-(origin-trunk × USA-code) aggregate (cached)
  GET  /api/cache-stats    - SQLite cache stats (auth'd)

Auth:
  Send X-Auth-Token: <token> header on /sql, /api/*. Token in /etc/cdr-direct-token.
"""
import subprocess
import hmac
import os
import re
import json
import time
import glob
import tempfile
from flask import (
    Flask, request, jsonify, Response, redirect, render_template, send_from_directory,
    stream_with_context,
)
from itsdangerous import BadData, URLSafeTimedSerializer

import cache  # local module — auto-inits SQLite on import
import db     # local module — DuckDB native DB
from settings import (
    CDR_ROOT, DUCKDB_BIN as DUCKDB, TOKEN_FILE, MAX_QUERY_DAYS,
    MAX_RESULT_ROWS, CSV_EXPORT_MAX_ROWS, ENABLE_SQL_ENDPOINT,
    DUCKDB_THREADS, DUCKDB_MEMORY_LIMIT,
)

# Initialize DuckDB native DB schema on import (idempotent)
try:
    db.init()
except Exception as _e:
    print('[startup] db.init() failed:', _e)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, 'templates'),
    static_folder=os.path.join(BASE_DIR, 'static'),
)

AUTH_TOKEN = os.environ.get('CDR_AUTH_TOKEN', '').strip()
if not AUTH_TOKEN:
    try:
        with open(TOKEN_FILE, 'r') as f:
            AUTH_TOKEN = f.read().strip()
    except OSError:
        # Fail closed: authenticated endpoints reject every request until a
        # token is configured. Imports still work for local checks and tests.
        AUTH_TOKEN = ''
        print('[startup] warning: no auth token configured at', TOKEN_FILE)

ALLOWED_PREFIXES = ('select', 'with', 'describe', 'show')
ENTITIES = ('MyCallConnect', 'SalamTalk', 'Dialphone', 'Vestacall')

# Granular switch termination reasons (CSV col 36 `reason`). This is the field
# that disambiguates a generic SIP 503 into its real internal cause. Kept as a
# whitelist regex so the free-text values can never be used for SQL injection
# (reason IN (...) takes string literals).
REASON_TOKEN_RE = re.compile(r'^[A-Z0-9_]{1,40}$')
SORT_FIELDS = {
    'customer', 'code', 'state', 'ratecenter', 'x5u_url', 'attest',
    'attempts', 'completions', 'asr_pct', 'minutes', 'revenue', 'cost', 'margin',
}
QUICK_FILTERS = {'fas-suspect', 'profitable'}


def sanitize_reasons(raw):
    """Return a de-duped list of syntactically valid reason tokens (UPPER_SNAKE)."""
    if not raw or not isinstance(raw, list):
        return []
    out, seen = [], set()
    for r in raw:
        tok = str(r).strip().upper()
        if REASON_TOKEN_RE.match(tok) and tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


def reason_clause(reasons):
    """Build a safe `reason IN ('A','B')` fragment, or '' if no valid reasons."""
    toks = sanitize_reasons(reasons)
    if not toks:
        return ''
    quoted = ','.join("'{}'".format(t) for t in toks)  # tokens are [A-Z0-9_] only
    return "reason IN ({})".format(quoted)


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────
def check_auth():
    """Return True if X-Auth-Token header matches stored token."""
    provided = request.headers.get('X-Auth-Token', '')
    return hmac.compare_digest(provided.encode(), AUTH_TOKEN.encode())


def run_duckdb(sql, timeout=600):
    """Run a DuckDB query, return (rows, error)."""
    try:
        configured_sql = (
            "PRAGMA threads={}; PRAGMA memory_limit='{}'; {}".format(
                DUCKDB_THREADS, DUCKDB_MEMORY_LIMIT, sql,
            )
        )
        result = subprocess.run(
            [DUCKDB, '-json', '-c', configured_sql],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=timeout, cwd=CDR_ROOT,
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


def build_csv_glob(entities, start_date, end_date, start_hour=None, end_hour=None):
    """
    Build a SQL-quoted list of existing CSV files for an entity/date range.

    Resolving wildcards here is important: DuckDB rejects the entire input
    list when even one daily wildcard matches no files. Live/partial days and
    entities that legitimately have no traffic must not break the other files.

    If start_hour/end_hour given (both 0..23 inclusive, UTC), only files for
    those hours are included — 46labs writes hourly files bucketed by
    call_end_time, so file 14.csv.gz holds the 14:00-14:59 UTC window.
    """
    from datetime import datetime, timedelta
    sd = datetime.strptime(start_date, '%Y-%m-%d').date()
    ed = datetime.strptime(end_date, '%Y-%m-%d').date()
    if ed < sd:
        sd, ed = ed, sd

    hours = None
    if start_hour is not None and end_hour is not None:
        try:
            sh, eh = int(start_hour), int(end_hour)
            if 0 <= sh <= 23 and 0 <= eh <= 23:
                if sh <= eh:
                    hours = list(range(sh, eh + 1))
                else:
                    hours = list(range(sh, 24)) + list(range(0, eh + 1))
        except (TypeError, ValueError):
            hours = None

    files = []
    day = sd
    while day <= ed:
        for entity in entities:
            if entity not in ENTITIES:
                continue
            base = "{}/{}/{:04d}/{:02d}/{:02d}".format(
                CDR_ROOT, entity, day.year, day.month, day.day,
            )
            if hours is None:
                patterns = ["{}/*.csv.gz".format(base)]
            else:
                patterns = ["{}/{:02d}.csv.gz".format(base, h) for h in hours]
            for pattern in patterns:
                for path in sorted(glob.glob(pattern)):
                    safe_path = os.path.abspath(path).replace('\\', '/').replace("'", "''")
                    files.append("'{}'".format(safe_path))
        day += timedelta(days=1)
    return files


def validate_body(body):
    """Common body validation. Returns (parsed_kwargs, err_response_tuple)."""
    from datetime import datetime

    if not isinstance(body, dict):
        return None, ({'error': 'JSON body must be an object'}, 400)
    start_date = body.get('start_date')
    end_date = body.get('end_date') or start_date

    raw_entities = body.get('entities')
    if raw_entities is None:
        entities = list(ENTITIES)
    elif isinstance(raw_entities, list) and len(raw_entities) == 0:
        return None, ({'error': 'no entities selected — pick at least one of MCC/ST/DP/VC'}, 400)
    if not start_date:
        return None, ({'error': 'start_date required'}, 400)

    try:
        sd = datetime.strptime(str(start_date), '%Y-%m-%d').date()
        ed = datetime.strptime(str(end_date), '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return None, ({'error': 'dates must use YYYY-MM-DD'}, 400)
    if ed < sd:
        return None, ({'error': 'end_date must be on or after start_date'}, 400)
    if (ed - sd).days + 1 > MAX_QUERY_DAYS:
        return None, ({'error': 'date range exceeds {} days'.format(MAX_QUERY_DAYS)}, 400)

    if raw_entities is not None:
        if not isinstance(raw_entities, list):
            return None, ({'error': 'entities must be a list'}, 400)
        unknown = [e for e in raw_entities if e not in ENTITIES]
        if unknown:
            return None, ({'error': 'unknown entities: {}'.format(', '.join(map(str, unknown)))}, 400)
        entities = list(dict.fromkeys(raw_entities))

    raw_sips = body.get('sip_codes') or []
    if not isinstance(raw_sips, list):
        return None, ({'error': 'sip_codes must be a list'}, 400)
    sip_codes = []
    try:
        for value in raw_sips:
            code = int(value)
            if not 100 <= code <= 699:
                raise ValueError
            if code not in sip_codes:
                sip_codes.append(code)
    except (TypeError, ValueError):
        return None, ({'error': 'sip_codes must contain values from 100 to 699'}, 400)

    # Keep the request key named `customer` for API compatibility, but its
    # value now identifies orig_trunk_group_name as requested.
    customer = body.get('customer')
    if customer is not None:
        customer = str(customer).strip() or None
        if customer and len(customer) > 255:
            return None, ({'error': 'origin trunk is too long'}, 400)

    start_hour, end_hour = body.get('start_hour'), body.get('end_hour')
    if (start_hour is None) != (end_hour is None):
        return None, ({'error': 'start_hour and end_hour must be provided together'}, 400)
    if start_hour is not None:
        try:
            start_hour, end_hour = int(start_hour), int(end_hour)
        except (TypeError, ValueError):
            return None, ({'error': 'hours must be integers from 0 to 23'}, 400)
        if not (0 <= start_hour <= 23 and 0 <= end_hour <= 23):
            return None, ({'error': 'hours must be integers from 0 to 23'}, 400)

    is_export = body.get('_export') is True
    limit_ceiling = CSV_EXPORT_MAX_ROWS + 1 if is_export else MAX_RESULT_ROWS
    try:
        limit = int(body.get('limit', limit_ceiling if is_export else MAX_RESULT_ROWS))
    except (TypeError, ValueError):
        return None, ({'error': 'limit must be an integer'}, 400)
    limit = max(1, min(limit, limit_ceiling))

    sort_by = str(body.get('sort_by') or 'revenue')
    if sort_by not in SORT_FIELDS:
        return None, ({'error': 'unsupported sort field'}, 400)
    sort_dir = str(body.get('sort_dir') or 'desc').lower()
    if sort_dir not in ('asc', 'desc'):
        return None, ({'error': 'sort_dir must be asc or desc'}, 400)
    quick_filter = body.get('quick_filter') or None
    if quick_filter not in QUICK_FILTERS and quick_filter is not None:
        return None, ({'error': 'unsupported quick_filter'}, 400)

    return {
        'start_date': sd.isoformat(),
        'end_date': ed.isoformat(),
        'entities': entities,
        'sip_codes': sip_codes,
        'reasons': sanitize_reasons(body.get('reasons')),
        'customer': customer,
        'start_hour': start_hour,
        'end_hour': end_hour,
        'limit': limit,
        'sort_by': sort_by,
        'sort_dir': sort_dir,
        'quick_filter': quick_filter,
    }, None


def build_where(sip_codes, customer, reasons=None):
    where = ["to_did LIKE '1%'", "length(to_did) = 11"]
    if sip_codes:
        codes = ','.join(str(int(c)) for c in sip_codes if isinstance(c, int) or str(c).isdigit())
        if codes:
            where.append("sip_code IN ({})".format(codes))
    rc = reason_clause(reasons)
    if rc:
        where.append(rc)
    if customer:
        safe = customer.replace("'", "''")
        where.append("orig_trunk_group_name = '{}'".format(safe))
    return ' AND '.join(where)


def all_entities_days_in_db(entities, start_date, end_date):
    """
    Return True only when every raw hourly file currently present for the
    request has a matching ingest_log entry.

    The old implementation considered a whole day complete after seeing just
    one ingested hour. During partial/live ingestion that silently omitted the
    other 23 hours from dashboard totals. If raw files have already been
    archived, day-level log coverage remains the compatibility fallback.
    """
    from datetime import datetime, timedelta
    try:
        sd = datetime.strptime(start_date, '%Y-%m-%d').date()
        ed = datetime.strptime(end_date, '%Y-%m-%d').date()
    except Exception:
        return False
    # This function intentionally checks every hour; hour-filtered requests are
    # cheap enough that a conservative full-day completeness check is safer.
    source_stats = {}
    scan_day = sd
    while scan_day <= ed:
        for entity in entities:
            base = os.path.join(
                CDR_ROOT, entity, '{:04d}'.format(scan_day.year),
                '{:02d}'.format(scan_day.month), '{:02d}'.format(scan_day.day),
            )
            for path in glob.glob(os.path.join(base, '*.csv.gz')):
                match = re.match(r'^(\d{2})\.csv\.gz$', os.path.basename(path))
                if match and 0 <= int(match.group(1)) <= 23:
                    file_hour = datetime(
                        scan_day.year, scan_day.month, scan_day.day,
                        int(match.group(1)), 0, 0,
                    ).isoformat(' ')
                    try:
                        stat = os.stat(path)
                        source_stats[(entity, file_hour)] = (stat.st_size, stat.st_mtime_ns)
                    except OSError:
                        return False
        scan_day += timedelta(days=1)

    if source_stats:
        quoted_pairs = ','.join(
            "('{}', TIMESTAMP '{}')".format(entity.replace("'", "''"), file_hour)
            for entity, file_hour in sorted(source_stats)
        )
        sql = """
        WITH needed(entity, file_hour) AS (VALUES {pairs})
        SELECT n.entity, n.file_hour, i.source_size, i.source_mtime_ns
        FROM needed n
        LEFT JOIN ingest_log i
          ON i.entity = n.entity AND i.file_hour = n.file_hour
        """.format(pairs=quoted_pairs)
        rows, query_err = db.run_query(sql, timeout=15)
        if query_err or len(rows or []) != len(source_stats):
            return False
        source_stats_by_hour = {
            (entity, file_hour[:13]): value
            for (entity, file_hour), value in source_stats.items()
        }
        for row in rows:
            key = (
                row.get('entity'),
                str(row.get('file_hour')).replace('T', ' ')[:13],
            )
            expected = source_stats_by_hour.get(key)
            if expected is None:
                return False
            if row.get('source_size') is None or row.get('source_mtime_ns') is None:
                return False
            if (int(row['source_size']), int(row['source_mtime_ns'])) != expected:
                return False
        return True

    # No raw files remain on disk (for example after archival). Fall back to
    # the historical day-level coverage check.
    needed = []
    d = sd
    while d <= ed:
        for e in entities:
            needed.append((e, d.isoformat()))
        d += timedelta(days=1)
    # One DB query: does ingest_log have at least one row for each (entity, day)?
    quoted_pairs = ','.join("('{}', DATE '{}')".format(e.replace("'", "''"), day) for e, day in needed)
    sql = """
    WITH needed(entity, day) AS (VALUES {pairs})
    SELECT n.entity AS missing_entity, n.day AS missing_day
    FROM needed n
    LEFT JOIN (
      SELECT DISTINCT entity, CAST(file_hour AS DATE) AS day
      FROM ingest_log
    ) i ON i.entity = n.entity AND i.day = n.day
    WHERE i.entity IS NULL
    LIMIT 1
    """.format(pairs=quoted_pairs)
    # Short timeout — if DB is locked by backfill, fall back instantly
    rows, query_err = db.run_query(sql, timeout=15)
    if query_err:
        return False  # be safe — fall back to CSV (likely backfill holds lock)
    return len(rows or []) == 0


def build_db_where(entities, start_date, end_date, sip_codes, customer,
                   start_hour=None, end_hour=None, reasons=None):
    """
    WHERE clause for queries against the cdr_records table.
    Filters by entity + file_hour range (clustering key) — yields massive
    chunk pruning via DuckDB zone maps.
    """
    from datetime import datetime, timedelta
    sd = datetime.strptime(start_date, '%Y-%m-%d')
    ed = datetime.strptime(end_date, '%Y-%m-%d')

    # End-of-day for ed (exclusive upper bound)
    ed_exclusive = ed + timedelta(days=1)

    parts = []
    # Entity filter — quoted, comma-separated
    quoted = ', '.join("'{}'".format(e.replace("'", "''")) for e in entities)
    parts.append("entity IN ({})".format(quoted))
    parts.append("file_hour >= TIMESTAMP '{}'".format(sd.isoformat(' ')))
    parts.append("file_hour <  TIMESTAMP '{}'".format(ed_exclusive.isoformat(' ')))

    parts.append("to_did LIKE '1%'")
    parts.append("length(to_did) = 11")
    if sip_codes:
        codes = ','.join(str(int(c)) for c in sip_codes if isinstance(c, int) or str(c).isdigit())
        if codes:
            parts.append("sip_code IN ({})".format(codes))
    rc = reason_clause(reasons)
    if rc:
        parts.append(rc)
    if customer:
        safe = customer.replace("'", "''")
        parts.append("orig_trunk_group_name = '{}'".format(safe))

    # Hour filter — applied on call_end_time (matches 46labs bucketing).
    # SKIP the clause entirely on full 0..23 span — it's always-true and
    # forces DuckDB to evaluate extract(hour) per row, killing scan speed
    # (adds ~5-6 min on 100M-row full-day 4-entity aggregations).
    if start_hour is not None and end_hour is not None:
        try:
            sh, eh = int(start_hour), int(end_hour)
            if 0 <= sh <= 23 and 0 <= eh <= 23:
                full_day = (sh == 0 and eh == 23)
                if not full_day:
                    if sh <= eh:
                        parts.append("extract(hour FROM call_end_time) BETWEEN {} AND {}".format(sh, eh))
                    else:
                        parts.append("(extract(hour FROM call_end_time) >= {} OR extract(hour FROM call_end_time) <= {})".format(sh, eh))
        except (TypeError, ValueError):
            pass

    return ' AND '.join(parts)


def filter_and_sort_rows(rows, parsed):
    """Apply validated table controls to an already aggregated row list."""
    quick_filter = parsed.get('quick_filter')
    if quick_filter == 'fas-suspect':
        rows = [r for r in rows if (r.get('asr_pct') or 0) < 15 and (r.get('attempts') or 0) >= 50]
    elif quick_filter == 'profitable':
        rows = [r for r in rows if (r.get('margin') or 0) > 0]

    sort_by = parsed.get('sort_by') or 'revenue'
    reverse = parsed.get('sort_dir') != 'asc'

    def sort_key(row):
        value = row.get(sort_by)
        if isinstance(value, str):
            return (value == '', value.lower())
        return (value is None, value if value is not None else 0)

    rows.sort(key=sort_key, reverse=reverse)
    return rows


# ─────────────────────────────────────────────────────────────────────
# Computation — pure functions, callable from API or refresher
# ─────────────────────────────────────────────────────────────────────
def compute_usa_codes(body):
    """
    Per-USA-code aggregate. Tries DuckDB native DB first; falls back to
    read_csv_auto on raw CSV.gz files if any (entity × day) isn't in ingest_log.
    """
    parsed, err = validate_body(body)
    if err:
        return {**err[0], '_http_status': err[1]}

    use_db = all_entities_days_in_db(
        parsed['entities'], parsed['start_date'], parsed['end_date'],
    )

    if use_db:
        where_sql = build_db_where(
            parsed['entities'], parsed['start_date'], parsed['end_date'],
            parsed['sip_codes'], parsed['customer'],
            parsed['start_hour'], parsed['end_hour'],
            reasons=parsed['reasons'],
        )
        sql = """
        SELECT
          substring(to_did, 2, 6) AS code,
          COALESCE(term_state, '?') AS state,
          COALESCE(term_ratecenter, '?') AS ratecenter,
          COUNT(*) AS attempts,
          COUNT(*) FILTER (WHERE sip_code = 200) AS completions,
          ROUND(100.0 * COUNT(*) FILTER (WHERE sip_code = 200) / NULLIF(COUNT(*), 0), 2) AS asr_pct,
          ROUND(SUM(orig_billed_duration) FILTER (WHERE sip_code = 200) / 60.0, 2) AS minutes,
          ROUND(SUM(orig_cost), 4) AS revenue,
          ROUND(SUM(term_cost), 4) AS cost,
          ROUND(SUM(orig_cost) - SUM(term_cost), 4) AS margin
        FROM cdr_records
        WHERE {where}
        GROUP BY code, state, ratecenter
        ORDER BY revenue DESC
        """.format(where=where_sql)
        rows, err = db.run_query(sql, timeout=600)
    else:
        # CSV fallback — same as pre-DuckDB-native path
        globs = build_csv_glob(
            parsed['entities'], parsed['start_date'], parsed['end_date'],
            parsed['start_hour'], parsed['end_hour'],
        )
        if not globs:
            return {'error': 'no entities matched ENTITIES whitelist', '_http_status': 400}
        glob_list = '[' + ','.join(globs) + ']'
        where_sql = build_where(parsed['sip_codes'], parsed['customer'], parsed['reasons'])
        sql = """
        SELECT
          substring(to_did, 2, 6) AS code,
          COALESCE(term_state, '?') AS state,
          COALESCE(term_ratecenter, '?') AS ratecenter,
          COUNT(*) AS attempts,
          COUNT(*) FILTER (WHERE sip_code = 200) AS completions,
          ROUND(100.0 * COUNT(*) FILTER (WHERE sip_code = 200) / NULLIF(COUNT(*), 0), 2) AS asr_pct,
          ROUND(SUM(orig_billed_duration) FILTER (WHERE sip_code = 200) / 60.0, 2) AS minutes,
          ROUND(SUM(orig_cost), 4) AS revenue,
          ROUND(SUM(term_cost), 4) AS cost,
          ROUND(SUM(orig_cost) - SUM(term_cost), 4) AS margin
        FROM read_csv_auto({glob}, union_by_name=true, ignore_errors=true, types={{'to_did': 'VARCHAR', 'from_did': 'VARCHAR', 'lrn_did': 'VARCHAR', 'callid': 'VARCHAR'}})
        WHERE {where}
        GROUP BY code, state, ratecenter
        ORDER BY revenue DESC
        """.format(glob=glob_list, where=where_sql)
        rows, err = run_duckdb(sql, timeout=600)
    if err is not None:
        return {'error': err, 'sql': sql, '_http_status': 400}

    totals = {
        'attempts': 0, 'completions': 0, 'minutes': 0.0,
        'revenue': 0.0, 'cost': 0.0, 'margin': 0.0, 'code_count': len(rows),
    }
    for r in rows:
        totals['attempts'] += r.get('attempts') or 0
        totals['completions'] += r.get('completions') or 0
        totals['minutes'] += r.get('minutes') or 0
        totals['revenue'] += r.get('revenue') or 0
        totals['cost'] += r.get('cost') or 0
        totals['margin'] += r.get('margin') or 0
    totals['asr_pct'] = round(100.0 * totals['completions'] / totals['attempts'], 2) if totals['attempts'] else 0

    total_row_count = len(rows)
    lim = parsed.get('limit', 5000)
    if lim > 0 and total_row_count > lim:
        rows = rows[:lim]  # already ORDER BY revenue DESC in SQL
    totals['code_count'] = total_row_count
    return {'totals': totals, 'rows': rows, 'row_count': len(rows), 'total_row_count': total_row_count}


def compute_usa_customer_codes(body):
    """Per-(origin trunk, USA-code) — DB native if covered, CSV fallback otherwise."""
    parsed, err = validate_body(body)
    if err:
        return {**err[0], '_http_status': err[1]}

    use_db = all_entities_days_in_db(
        parsed['entities'], parsed['start_date'], parsed['end_date'],
    )

    if use_db:
        where_sql = build_db_where(
            parsed['entities'], parsed['start_date'], parsed['end_date'],
            parsed['sip_codes'], parsed['customer'],
            parsed['start_hour'], parsed['end_hour'],
            reasons=parsed['reasons'],
        )
        # SPLIT into two DuckDB queries to avoid piping millions of rows
        # to Python. Query A = totals over ALL groups (1 row); Query B = top
        # 5000 by revenue (SQL LIMIT). Cuts response 280s -> ~25s on
        # 4-entity full-day queries.
        lim_early = parsed['limit']
        base_group_sql = """
        SELECT
          COALESCE(orig_trunk_group_name, '(none)') AS customer,
          substring(to_did, 2, 6) AS code,
          COALESCE(term_state, '?') AS state,
          COALESCE(term_ratecenter, '?') AS ratecenter,
          ANY_VALUE(COALESCE(NULLIF(stir_x5u, ''), '(unsigned)')) AS x5u_url,
          ANY_VALUE(COALESCE(NULLIF(stir_attest, ''), '?')) AS attest,
          COUNT(*) AS attempts,
          COUNT(*) FILTER (WHERE sip_code = 200) AS completions,
          ROUND(100.0 * COUNT(*) FILTER (WHERE sip_code = 200) / NULLIF(COUNT(*), 0), 2) AS asr_pct,
          ROUND(SUM(orig_billed_duration) FILTER (WHERE sip_code = 200) / 60.0, 2) AS minutes,
          ROUND(SUM(orig_cost), 4) AS revenue,
          ROUND(SUM(term_cost), 4) AS cost,
          ROUND(SUM(orig_cost) - SUM(term_cost), 4) AS margin
        FROM cdr_records
        WHERE {where}
        GROUP BY customer, code, state, ratecenter""".format(where=where_sql)

        totals_sql = """
        SELECT
          COUNT(*) AS pair_count,
          COUNT(DISTINCT customer) AS customer_count,
          COUNT(DISTINCT code) AS code_count,
          LIST(DISTINCT customer ORDER BY customer) AS customers,
          SUM(attempts) AS attempts,
          SUM(completions) AS completions,
          SUM(minutes) AS minutes,
          SUM(revenue) AS revenue,
          SUM(cost) AS cost,
          SUM(margin) AS margin
        FROM (""" + base_group_sql + """)"""

        row_filter = ''
        if parsed['quick_filter'] == 'fas-suspect':
            row_filter = 'WHERE asr_pct < 15 AND attempts >= 50'
        elif parsed['quick_filter'] == 'profitable':
            row_filter = 'WHERE margin > 0'
        top_sql = """
        SELECT *, COUNT(*) OVER () AS filtered_row_count
        FROM ({base}) grouped
        {row_filter}
        ORDER BY {sort_by} {sort_dir}, customer ASC, code ASC
        LIMIT {limit}
        """.format(
            base=base_group_sql,
            row_filter=row_filter,
            sort_by=parsed['sort_by'],
            sort_dir=parsed['sort_dir'].upper(),
            limit=lim_early,
        )

        totals_rows, err = db.run_query(totals_sql, timeout=300)
        if err is not None:
            return {'error': err, 'sql': totals_sql, '_http_status': 400}
        rows, err = db.run_query(top_sql, timeout=300)
        if err is not None:
            return {'error': err, 'sql': top_sql, '_http_status': 400}

        # Build response inline (bypass slow Python for-loop over all rows)
        tr = (totals_rows or [{}])[0]
        totals = {
          'attempts': int(tr.get('attempts') or 0),
          'completions': int(tr.get('completions') or 0),
          'minutes': float(tr.get('minutes') or 0),
          'revenue': float(tr.get('revenue') or 0),
          'cost': float(tr.get('cost') or 0),
          'margin': float(tr.get('margin') or 0),
          'pair_count': int(tr.get('pair_count') or 0),
          'customer_count': int(tr.get('customer_count') or 0),
          'code_count': int(tr.get('code_count') or 0),
          'customers': tr.get('customers') or [],
        }
        totals['asr_pct'] = round(100.0 * totals['completions'] / totals['attempts'], 2) if totals['attempts'] else 0
        filtered_row_count = int(rows[0].get('filtered_row_count') or 0) if rows else 0
        for row in rows:
            row.pop('filtered_row_count', None)
        return {
            'totals': totals,
            'customers': totals['customers'],
            'rows': rows,
            'row_count': len(rows),
            'total_row_count': filtered_row_count,
        }
    else:
        globs = build_csv_glob(
            parsed['entities'], parsed['start_date'], parsed['end_date'],
            parsed['start_hour'], parsed['end_hour'],
        )
        if not globs:
            return {'error': 'no entities matched ENTITIES whitelist', '_http_status': 400}
        glob_list = '[' + ','.join(globs) + ']'
        where_sql = build_where(parsed['sip_codes'], parsed['customer'], parsed['reasons'])
        sql = """
    SELECT
      COALESCE(orig_trunk_group_name, '(none)') AS customer,
      substring(to_did, 2, 6) AS code,
      COALESCE(term_state, '?') AS state,
      COALESCE(term_ratecenter, '?') AS ratecenter,
      ANY_VALUE(COALESCE(NULLIF(stir_x5u, ''), '(unsigned)')) AS x5u_url,
      ANY_VALUE(COALESCE(NULLIF(stir_attest, ''), '?')) AS attest,
      COUNT(*) AS attempts,
      COUNT(*) FILTER (WHERE sip_code = 200) AS completions,
      ROUND(100.0 * COUNT(*) FILTER (WHERE sip_code = 200) / NULLIF(COUNT(*), 0), 2) AS asr_pct,
      ROUND(SUM(orig_billed_duration) FILTER (WHERE sip_code = 200) / 60.0, 2) AS minutes,
      ROUND(SUM(orig_cost), 4) AS revenue,
      ROUND(SUM(term_cost), 4) AS cost,
      ROUND(SUM(orig_cost) - SUM(term_cost), 4) AS margin
    FROM read_csv_auto({glob}, union_by_name=true, ignore_errors=true, types={{'to_did': 'VARCHAR', 'from_did': 'VARCHAR', 'lrn_did': 'VARCHAR', 'callid': 'VARCHAR', 'stir_x5u': 'VARCHAR', 'stir_attest': 'VARCHAR', 'stir_orig_id': 'VARCHAR', 'stir_orig_tn': 'VARCHAR', 'stir_dest_tn': 'VARCHAR', 'p_charge_info': 'VARCHAR'}})
    WHERE {where}
    GROUP BY customer, code, state, ratecenter
    ORDER BY revenue DESC
    """.format(glob=glob_list, where=where_sql)
        rows, err = run_duckdb(sql, timeout=600)
    if err is not None:
        return {'error': err, 'sql': sql, '_http_status': 400}

    totals = {
        'attempts': 0, 'completions': 0, 'minutes': 0.0,
        'revenue': 0.0, 'cost': 0.0, 'margin': 0.0,
        'pair_count': len(rows),
    }
    seen_customers = set()
    seen_codes = set()
    for r in rows:
        totals['attempts'] += r.get('attempts') or 0
        totals['completions'] += r.get('completions') or 0
        totals['minutes'] += r.get('minutes') or 0
        totals['revenue'] += r.get('revenue') or 0
        totals['cost'] += r.get('cost') or 0
        totals['margin'] += r.get('margin') or 0
        if r.get('customer'):
            seen_customers.add(r['customer'])
        if r.get('code'):
            seen_codes.add(r['code'])
    totals['customer_count'] = len(seen_customers)
    totals['code_count'] = len(seen_codes)
    totals['customers'] = sorted(seen_customers)
    totals['asr_pct'] = round(100.0 * totals['completions'] / totals['attempts'], 2) if totals['attempts'] else 0

    rows = filter_and_sort_rows(rows, parsed)
    total_row_count = len(rows)
    lim = parsed.get('limit', 5000)
    if lim > 0 and total_row_count > lim:
        rows = rows[:lim]
    return {
        'totals': totals,
        'customers': totals['customers'],
        'rows': rows,
        'row_count': len(rows),
        'total_row_count': total_row_count,
    }


# ─────────────────────────────────────────────────────────────────────
# Cache wrapper — used by both API endpoints
# ─────────────────────────────────────────────────────────────────────
def cached_compute(endpoint, body, compute_fn, force=False):
    """
    Stale-while-revalidate semantics:
      - cache miss → compute synchronously, store, return
      - cache fresh → return cached immediately
      - cache stale → return cached + flag stale (background refresher repopulates)
      - force=True → bypass cache, recompute, store
    """
    cache_key = cache.make_key(endpoint, body)
    tier = cache.classify_tier(body)

    if not force:
        cached = cache.get(cache_key)
        if cached:
            resp = dict(cached['response'])
            resp['_cache'] = {
                'hit': True,
                'refreshed_at': cached['refreshed_at'],
                'age_seconds': int(cached['age_seconds']),
                'tier': cached['tier'],
                'ttl': cached['ttl'],
                'stale': cached['stale'],
                'compute_ms': cached['compute_ms'],
            }
            return resp, 200

    # Cache miss (or force) — compute fresh
    t0 = time.time()
    result = compute_fn(body)
    compute_ms = int((time.time() - t0) * 1000)

    if 'error' in result:
        status = result.pop('_http_status', 400)
        return result, status

    cache.put(cache_key, endpoint, body, result, tier, compute_ms)
    result['_cache'] = {
        'hit': False,
        'refreshed_at': time.time(),
        'age_seconds': 0,
        'tier': tier,
        'ttl': cache.TIER_TTLS.get(tier, 86400),
        'stale': False,
        'compute_ms': compute_ms,
    }
    return result, 200


# ─────────────────────────────────────────────────────────────────────
# Public endpoints
# ─────────────────────────────────────────────────────────────────────


def compute_stir_x5u(body):
    """Per-STIR-X5U aggregate. CSV-only — DuckDB table lacks stir_* columns.
    Slower than DuckDB paths (40-90s for full-day scans)."""
    parsed, err = validate_body(body)
    if err:
        return {**err[0], '_http_status': err[1]}

    globs = build_csv_glob(
        parsed['entities'], parsed['start_date'], parsed['end_date'],
        parsed['start_hour'], parsed['end_hour'],
    )
    if not globs:
        return {'error': 'no entities matched ENTITIES whitelist', '_http_status': 400}
    glob_list = '[' + ','.join(globs) + ']'
    where_sql = build_where(parsed['sip_codes'], parsed['customer'], parsed['reasons'])
    sql = """
    SELECT
      COALESCE(NULLIF(stir_x5u, ''), '(unsigned)') AS x5u_url,
      COALESCE(NULLIF(stir_attest, ''), '?') AS attest,
      COUNT(*) AS attempts,
      COUNT(*) FILTER (WHERE sip_code = 200) AS completions,
      ROUND(100.0 * COUNT(*) FILTER (WHERE sip_code = 200) / NULLIF(COUNT(*), 0), 2) AS asr_pct,
      ROUND(SUM(orig_billed_duration) FILTER (WHERE sip_code = 200) / 60.0, 2) AS minutes,
      ROUND(SUM(orig_cost), 4) AS revenue,
      ROUND(SUM(term_cost), 4) AS cost,
      ROUND(SUM(orig_cost) - SUM(term_cost), 4) AS margin
    FROM read_csv_auto({glob}, union_by_name=true, ignore_errors=true, types={{'to_did': 'VARCHAR', 'from_did': 'VARCHAR', 'lrn_did': 'VARCHAR', 'callid': 'VARCHAR', 'stir_x5u': 'VARCHAR', 'stir_attest': 'VARCHAR', 'stir_orig_id': 'VARCHAR', 'stir_orig_tn': 'VARCHAR', 'stir_dest_tn': 'VARCHAR', 'p_charge_info': 'VARCHAR'}})
    WHERE {where}
    GROUP BY x5u_url, attest
    ORDER BY revenue DESC
    """.format(glob=glob_list, where=where_sql)
    rows, err = run_duckdb(sql, timeout=600)
    if err is not None:
        return {'error': err, 'sql': sql, '_http_status': 400}

    totals = {'attempts': 0, 'completions': 0, 'minutes': 0.0,
              'revenue': 0.0, 'cost': 0.0, 'margin': 0.0}
    for r in rows:
        totals['attempts'] += r.get('attempts') or 0
        totals['completions'] += r.get('completions') or 0
        totals['minutes'] += r.get('minutes') or 0
        totals['revenue'] += r.get('revenue') or 0
        totals['cost'] += r.get('cost') or 0
        totals['margin'] += r.get('margin') or 0
    totals['asr_pct'] = round(100.0 * totals['completions'] / totals['attempts'], 2) if totals['attempts'] else 0

    total_row_count = len(rows)
    lim = parsed.get('limit', 5000)
    if lim > 0 and total_row_count > lim:
        rows = rows[:lim]
    totals['x5u_count'] = total_row_count
    return {'totals': totals, 'rows': rows, 'row_count': len(rows), 'total_row_count': total_row_count}


@app.route('/health', methods=['GET'])
def health():
    return jsonify({'ok': True, 'service': 'cdr-direct'})


@app.route('/ready', methods=['GET'])
def ready():
    """Deployment readiness for Docker/Coolify health checks."""
    checks = {
        'auth_token': bool(AUTH_TOKEN),
        'cdr_root': os.path.isdir(CDR_ROOT),
        'duckdb_cli': os.path.isfile(DUCKDB) and os.access(DUCKDB, os.X_OK),
        'database': os.path.isfile(db.DB_PATH),
    }
    ok = all(checks.values())
    return jsonify({'ok': ok, 'service': 'cdr-direct', 'checks': checks}), (200 if ok else 503)


@app.route('/', methods=['GET'])
def root():
    return redirect('/ui')


@app.route('/ui', methods=['GET'])
def ui():
    return render_template('index.html')


@app.route('/static/<path:filename>', methods=['GET'])
def static_files(filename):
    return send_from_directory(app.static_folder, filename)


@app.route('/sql', methods=['POST'])
def run_sql():
    if not check_auth():
        return jsonify({'error': 'unauthorized'}), 401
    if not ENABLE_SQL_ENDPOINT:
        return jsonify({'error': 'raw SQL endpoint is disabled'}), 404
    data = request.get_json(force=True, silent=True) or {}
    sql = (data.get('sql') or '').strip()
    if not sql:
        return jsonify({'error': 'sql field required'}), 400
    cleaned = re.sub(r'^(--[^\n]*\n|\s)+', '', sql).lower()
    first = cleaned.split()[0] if cleaned.split() else ''
    # Do not allow statement chaining. Prefix-only checks otherwise permit
    # "SELECT ...; COPY/ATTACH ..." and turn a read-only endpoint into a file
    # read/write primitive.
    unsafe_sql = re.search(
        r'\b(attach|copy|export|import|install|load|pragma|read_csv|read_json|'
        r'read_text|read_blob|read_parquet|parquet_scan|glob|sqlite_scan)\b',
        cleaned,
    )
    if first not in ALLOWED_PREFIXES or ';' in cleaned.rstrip(';') or unsafe_sql:
        return jsonify({
            'error': 'only one read-only {} query is allowed'.format(ALLOWED_PREFIXES),
        }), 400
    rows, err = run_duckdb(sql)
    if err is not None:
        return jsonify({'error': err}), 400
    return Response(json.dumps(rows), mimetype='application/json')


@app.route('/api/usa-codes', methods=['POST'])
def api_usa_codes():
    if not check_auth():
        return jsonify({'error': 'unauthorized'}), 401
    body = request.get_json(force=True, silent=True) or {}
    body.pop('_export', None)
    force = bool(body.get('force_refresh'))
    result, status = cached_compute('usa-codes', body, compute_usa_codes, force=force)
    return jsonify(result), status


@app.route('/api/usa-customer-codes', methods=['POST'])
def api_usa_customer_codes():
    if not check_auth():
        return jsonify({'error': 'unauthorized'}), 401
    body = request.get_json(force=True, silent=True) or {}
    body.pop('_export', None)
    force = bool(body.get('force_refresh'))
    result, status = cached_compute('usa-customer-codes', body, compute_usa_customer_codes, force=force)
    return jsonify(result), status


@app.route('/api/cache-stats', methods=['GET'])
def api_cache_stats():
    if not check_auth():
        return jsonify({'error': 'unauthorized'}), 401
    return jsonify(cache.stats())


@app.route('/api/db-stats', methods=['GET'])
def api_db_stats():
    if not check_auth():
        return jsonify({'error': 'unauthorized'}), 401
    return jsonify(db.stats())


# Backward-compat: kept for any legacy client; uses cached compute
@app.route('/api/customers', methods=['POST'])
def api_customers():
    """Deprecated — returns origin trunks for backward API compatibility."""
    if not check_auth():
        return jsonify({'error': 'unauthorized'}), 401
    body = request.get_json(force=True, silent=True) or {}
    parsed, err = validate_body(body)
    if err:
        return jsonify(err[0]), err[1]

    where_sql = build_db_where(
        parsed['entities'], parsed['start_date'], parsed['end_date'],
        [], None,
        parsed['start_hour'], parsed['end_hour'],
    )
    sql = """
    SELECT DISTINCT orig_trunk_group_name AS name
    FROM cdr_records
    WHERE {where} AND orig_trunk_group_name IS NOT NULL AND orig_trunk_group_name != ''
    ORDER BY name
    """.format(where=where_sql)
    rows, err = db.run_query(sql, timeout=600)
    if err is not None:
        return jsonify({'error': err}), 400
    return jsonify({'customers': [r.get('name') for r in rows]})



# ─── CSV export endpoints ────────────────────────────────────────────────
def csv_response(rows, columns, filename):
    """Stream CSV encoding so the response is not duplicated in memory."""
    import io
    import csv as _csv

    @stream_with_context
    def generate():
        out = io.StringIO()
        writer = _csv.DictWriter(out, fieldnames=columns, extrasaction='ignore')
        writer.writeheader()
        yield out.getvalue()
        for row in rows:
            out.seek(0)
            out.truncate(0)
            writer.writerow(row)
            yield out.getvalue()

    return Response(
        generate(), mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename="{}"'.format(filename)},
    )


def build_customer_codes_export_sql(parsed, use_db):
    """Build an unlimited, validated aggregate query for streamed CSV export."""
    if use_db:
        source_sql = 'cdr_records'
        where_sql = build_db_where(
            parsed['entities'], parsed['start_date'], parsed['end_date'],
            parsed['sip_codes'], parsed['customer'],
            parsed['start_hour'], parsed['end_hour'],
            reasons=parsed['reasons'],
        )
    else:
        globs = build_csv_glob(
            parsed['entities'], parsed['start_date'], parsed['end_date'],
            parsed['start_hour'], parsed['end_hour'],
        )
        if not globs:
            return None, 'no raw CDR files were found for the selected period'
        type_overrides = ', '.join(
            "'{}': '{}'".format(name, value)
            for name, value in db.TYPE_OVERRIDES.items()
        )
        source_sql = (
            "read_csv_auto([{files}], union_by_name=true, ignore_errors=true, "
            "types={{ {types} }})"
        ).format(files=','.join(globs), types=type_overrides)
        where_sql = build_where(
            parsed['sip_codes'], parsed['customer'], parsed['reasons'],
        )

    grouped_sql = """
      SELECT
        COALESCE(orig_trunk_group_name, '(none)') AS origin_trunk,
        substring(to_did, 2, 6) AS code,
        COALESCE(term_state, '?') AS state,
        COALESCE(term_ratecenter, '?') AS ratecenter,
        ANY_VALUE(COALESCE(NULLIF(stir_x5u, ''), '(unsigned)')) AS x5u_url,
        ANY_VALUE(COALESCE(NULLIF(stir_attest, ''), '?')) AS attest,
        COUNT(*) AS attempts,
        COUNT(*) FILTER (WHERE sip_code = 200) AS completions,
        ROUND(100.0 * COUNT(*) FILTER (WHERE sip_code = 200) / NULLIF(COUNT(*), 0), 2) AS asr_pct,
        ROUND(SUM(orig_billed_duration) FILTER (WHERE sip_code = 200) / 60.0, 2) AS minutes,
        ROUND(SUM(orig_cost), 4) AS revenue,
        ROUND(SUM(term_cost), 4) AS cost,
        ROUND(SUM(orig_cost) - SUM(term_cost), 4) AS margin
      FROM {source}
      WHERE {where}
      GROUP BY origin_trunk, code, state, ratecenter
    """.format(source=source_sql, where=where_sql)

    row_filter = ''
    if parsed['quick_filter'] == 'fas-suspect':
        row_filter = 'WHERE asr_pct < 15 AND attempts >= 50'
    elif parsed['quick_filter'] == 'profitable':
        row_filter = 'WHERE margin > 0'

    sort_by = parsed['sort_by']
    if sort_by == 'customer':
        sort_by = 'origin_trunk'
    sql = """
      SELECT
        origin_trunk, code, state, ratecenter, x5u_url, attest,
        attempts, completions, asr_pct, minutes, revenue, cost, margin
      FROM ({grouped}) grouped
      {row_filter}
      ORDER BY {sort_by} {sort_dir}, origin_trunk ASC, code ASC
    """.format(
        grouped=grouped_sql,
        row_filter=row_filter,
        sort_by=sort_by,
        sort_dir=parsed['sort_dir'].upper(),
    )
    return sql, None


def stream_duckdb_csv(sql, filename, use_db):
    """Stream DuckDB COPY output without materializing the result in Python."""
    configured_sql = (
        "PRAGMA threads={}; PRAGMA memory_limit='{}'; "
        "COPY ({}) TO '/dev/stdout' WITH (FORMAT CSV, HEADER);"
    ).format(DUCKDB_THREADS, DUCKDB_MEMORY_LIMIT, sql)
    command = [DUCKDB]
    if use_db:
        command.append(db.DB_PATH)
    command.extend(['-c', configured_sql])

    error_log = tempfile.TemporaryFile()
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=error_log,
            bufsize=0,
        )
    except OSError as exc:
        error_log.close()
        return None, 'duckdb unavailable: {}'.format(exc)

    @stream_with_context
    def generate():
        return_code = None
        try:
            while True:
                chunk = process.stdout.read(256 * 1024)
                if not chunk:
                    break
                yield chunk
            return_code = process.wait()
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            process.stdout.close()
            if return_code not in (None, 0):
                error_log.seek(0)
                message = error_log.read(1000).decode('utf-8', errors='replace').strip()
                print('[csv-export] duckdb failed:', message or 'exit {}'.format(return_code))
            error_log.close()

    return Response(
        generate(),
        mimetype='text/csv',
        headers={
            'Content-Disposition': 'attachment; filename="{}"'.format(filename),
            'Cache-Control': 'no-store',
            'X-Accel-Buffering': 'no',
        },
    ), None


def csv_ticket_serializer():
    if not AUTH_TOKEN:
        return None
    return URLSafeTimedSerializer(AUTH_TOKEN, salt='cdr-direct-csv-v1')


def prepare_export(body, compute_fn):
    """Validate/cap an export and require the safe native-DB query path."""
    export_body = dict(body or {})
    export_body['_export'] = True
    export_body['limit'] = CSV_EXPORT_MAX_ROWS + 1
    parsed, validation_error = validate_body(export_body)
    if validation_error:
        return None, validation_error
    if not all_entities_days_in_db(
        parsed['entities'], parsed['start_date'], parsed['end_date'],
    ):
        return None, ({
            'error': 'export requires complete ingestion; raw-file fallback is disabled for safety'
        }, 409)
    result = compute_fn(export_body)
    if '_http_status' in result:
        status = result.pop('_http_status')
        return None, (result, status)
    if result.get('total_row_count', 0) > CSV_EXPORT_MAX_ROWS:
        return None, ({
            'error': 'export has {:,} rows; narrow the filters below the {:,}-row safety limit'.format(
                result['total_row_count'], CSV_EXPORT_MAX_ROWS,
            )
        }, 413)
    return result, None


@app.route('/api/usa-codes/csv', methods=['POST'])
def api_usa_codes_csv():
    if not check_auth():
        return jsonify({'error': 'unauthorized'}), 401
    body = request.get_json() or {}
    result, export_error = prepare_export(body, compute_usa_codes)
    if export_error:
        return jsonify(export_error[0]), export_error[1]
    cols = ['code', 'state', 'ratecenter', 'attempts', 'completions', 'asr_pct', 'minutes', 'revenue', 'cost', 'margin']
    sd = body.get('start_date', ''); ed = body.get('end_date', sd)
    fn = 'cdr_usa_codes_{}_to_{}.csv'.format(sd, ed)
    return csv_response(result.get('rows') or [], cols, fn)


@app.route('/api/usa-customer-codes/csv-ticket', methods=['POST'])
def api_usa_customer_codes_csv_ticket():
    if not check_auth():
        return jsonify({'error': 'unauthorized'}), 401
    body = request.get_json() or {}
    parsed, validation_error = validate_body(body)
    if validation_error:
        return jsonify(validation_error[0]), validation_error[1]
    serializer = csv_ticket_serializer()
    if serializer is None:
        return jsonify({'error': 'CSV export authentication is not configured'}), 503
    ticket = serializer.dumps(parsed)
    return jsonify({
        'download_url': '/api/usa-customer-codes/csv?ticket={}'.format(ticket),
        'expires_in': 600,
    })


@app.route('/api/usa-customer-codes/csv', methods=['GET', 'POST'])
def api_usa_customer_codes_csv():
    if request.method == 'GET':
        serializer = csv_ticket_serializer()
        if serializer is None:
            return jsonify({'error': 'CSV export authentication is not configured'}), 503
        try:
            body = serializer.loads(request.args.get('ticket', ''), max_age=600)
        except BadData:
            return jsonify({'error': 'CSV download link is invalid or expired'}), 401
    else:
        # Backward compatibility for older dashboard builds and API clients.
        if not check_auth():
            return jsonify({'error': 'unauthorized'}), 401
        body = request.get_json() or {}

    parsed, validation_error = validate_body(body)
    if validation_error:
        return jsonify(validation_error[0]), validation_error[1]

    use_db = all_entities_days_in_db(
        parsed['entities'], parsed['start_date'], parsed['end_date'],
    )
    sql, query_error = build_customer_codes_export_sql(parsed, use_db)
    if query_error:
        return jsonify({'error': query_error}), 404

    filename = 'cdr_usa_customer_codes_{}_to_{}.csv'.format(
        parsed['start_date'], parsed['end_date'],
    )
    response, stream_error = stream_duckdb_csv(sql, filename, use_db)
    if stream_error:
        return jsonify({'error': stream_error}), 503
    return response




@app.route('/api/stir-x5u', methods=['POST'])
def api_stir_x5u():
    if not check_auth():
        return jsonify({'error': 'unauthorized'}), 401
    body = request.get_json() or {}
    result = compute_stir_x5u(body)
    return jsonify(result), result.get('_http_status', 200)


@app.route('/api/stir-x5u/csv', methods=['POST'])
def api_stir_x5u_csv():
    if not check_auth():
        return jsonify({'error': 'unauthorized'}), 401
    body = request.get_json() or {}
    body['_export'] = True
    body['limit'] = CSV_EXPORT_MAX_ROWS + 1
    result = compute_stir_x5u(body)
    if '_http_status' in result:
        return jsonify({k: v for k, v in result.items() if k != '_http_status'}), result['_http_status']
    if result.get('total_row_count', 0) > CSV_EXPORT_MAX_ROWS:
        return jsonify({'error': 'export is too large; narrow the filters'}), 413
    cols = ['x5u_url', 'attest', 'attempts', 'completions', 'asr_pct', 'minutes', 'revenue', 'cost', 'margin']
    sd = body.get('start_date', ''); ed = body.get('end_date', sd)
    fn = 'cdr_stir_x5u_{}_to_{}.csv'.format(sd, ed)
    return csv_response(result.get('rows') or [], cols, fn)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8090)
