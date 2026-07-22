"""Runtime configuration for CDR Direct.

Production defaults preserve the current Linux deployment. Every storage path
can be overridden so large CDR data can live on a dedicated volume (for
example ``D:\\CDR`` on Windows) without changing application code.
"""
import os


def _int_env(name, default, minimum=1):
    try:
        return max(minimum, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


CDR_ROOT = os.path.abspath(os.environ.get('CDR_ROOT', '/root'))
APP_DATA_ROOT = os.path.abspath(os.environ.get('CDR_APP_DATA_ROOT', '/opt/cdr-direct'))
PARQUET_ROOT = os.path.abspath(
    os.environ.get('CDR_PARQUET_ROOT', os.path.join(CDR_ROOT, 'parquet'))
)
DB_PATH = os.path.abspath(
    os.environ.get('CDR_DB_PATH', os.path.join(APP_DATA_ROOT, 'cdrdata.duckdb'))
)
CACHE_DB = os.path.abspath(
    os.environ.get('CDR_CACHE_DB', os.path.join(APP_DATA_ROOT, 'cache.sqlite'))
)
DUCKDB_BIN = os.environ.get('CDR_DUCKDB_BIN', '/usr/local/bin/duckdb')
TOKEN_FILE = os.path.abspath(os.environ.get('CDR_TOKEN_FILE', '/etc/cdr-direct-token'))
DUCKDB_THREADS = _int_env('CDR_DUCKDB_THREADS', 8)
DUCKDB_MEMORY_LIMIT = os.environ.get('CDR_DUCKDB_MEMORY_LIMIT', '8GB').strip() or '8GB'

MAX_QUERY_DAYS = _int_env('CDR_MAX_QUERY_DAYS', 31)
MAX_RESULT_ROWS = _int_env('CDR_MAX_RESULT_ROWS', 5000)
CSV_EXPORT_MAX_ROWS = _int_env('CDR_CSV_EXPORT_MAX_ROWS', 100000)
ENABLE_SQL_ENDPOINT = os.environ.get('CDR_ENABLE_SQL', '').strip().lower() in {
    '1', 'true', 'yes', 'on',
}
