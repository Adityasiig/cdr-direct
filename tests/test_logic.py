import os
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock


TEST_RUNTIME = tempfile.mkdtemp(prefix='cdr-direct-tests-')
os.environ.setdefault('CDR_APP_DATA_ROOT', TEST_RUNTIME)
os.environ.setdefault('CDR_DB_PATH', os.path.join(TEST_RUNTIME, 'test.duckdb'))
os.environ.setdefault('CDR_CACHE_DB', os.path.join(TEST_RUNTIME, 'cache.sqlite'))
os.environ.setdefault('CDR_ROOT', os.path.join(TEST_RUNTIME, 'raw'))
os.environ.setdefault('CDR_AUTH_TOKEN', 'test-token')

import api
import cache


class ValidationTests(unittest.TestCase):
    def valid_body(self, **overrides):
        body = {
            'start_date': '2026-07-21',
            'end_date': '2026-07-21',
            'entities': ['MyCallConnect'],
            'sip_codes': [],
        }
        body.update(overrides)
        return body

    def test_default_outcome_scope_is_all_sip_codes(self):
        parsed, error = api.validate_body(self.valid_body())
        self.assertIsNone(error)
        self.assertEqual(parsed['sip_codes'], [])

    def test_rejects_unknown_entities_and_reverse_ranges(self):
        _, error = api.validate_body(self.valid_body(entities=['not-real']))
        self.assertEqual(error[1], 400)
        _, error = api.validate_body(self.valid_body(end_date='2026-07-20'))
        self.assertEqual(error[1], 400)

    def test_validates_hours_sip_codes_and_limit(self):
        _, error = api.validate_body(self.valid_body(start_hour=1))
        self.assertEqual(error[1], 400)
        _, error = api.validate_body(self.valid_body(sip_codes=[999]))
        self.assertEqual(error[1], 400)
        parsed, error = api.validate_body(self.valid_body(limit=999999))
        self.assertIsNone(error)
        self.assertEqual(parsed['limit'], api.MAX_RESULT_ROWS)

    def test_cache_key_includes_global_table_controls(self):
        base = self.valid_body(limit=5000, sort_by='revenue', sort_dir='desc')
        other = dict(base, sort_by='margin', sort_dir='asc', quick_filter='profitable')
        self.assertNotEqual(cache.make_key('usa-customer-codes', base),
                            cache.make_key('usa-customer-codes', other))

    def test_daily_snapshot_rejects_a_result_computed_before_finalization(self):
        snapshot_day = date(2026, 7, 22)
        body = cache.daily_snapshot_body(snapshot_day)
        key = cache.make_key('usa-customer-codes', body)
        too_early = datetime(2026, 7, 23, 1, 0, tzinfo=timezone.utc).timestamp()
        with mock.patch.object(cache.time, 'time', return_value=too_early):
            cache.put(
                key, 'usa-customer-codes', body, {'rows': []},
                cache.classify_tier(body), compute_ms=123,
            )

        prepared = cache.latest_daily_snapshot(
            today=date(2026, 7, 23), final_utc_hour=2,
        )

        self.assertIsNone(prepared)

    def test_latest_daily_snapshot_returns_prepared_full_day(self):
        snapshot_day = date(2026, 7, 22)
        body = cache.daily_snapshot_body(snapshot_day)
        response = {'rows': [{'code': '212555'}], 'totals': {'attempts': 1}}
        key = cache.make_key('usa-customer-codes', body)
        cache.put(
            key, 'usa-customer-codes', body, response,
            cache.classify_tier(body), compute_ms=123,
        )

        prepared = cache.latest_daily_snapshot(today=date(2026, 7, 23))

        self.assertEqual(prepared['snapshot_date'], '2026-07-22')
        self.assertEqual(prepared['response'], response)

    def test_origin_trunk_is_the_filter_dimension(self):
        where = api.build_where([], "MCC-TRUNK-A")
        self.assertIn("orig_trunk_group_name = 'MCC-TRUNK-A'", where)
        self.assertNotIn('orig_carrier_name', where)


class IngestionCoverageTests(unittest.TestCase):
    def test_changed_or_missing_hour_forces_raw_fallback(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root, 'MyCallConnect', '2026', '07', '21', '14.csv.gz')
            source.parent.mkdir(parents=True)
            source.write_bytes(b'cdr')
            stat = source.stat()
            complete_row = [{
                'entity': 'MyCallConnect',
                'file_hour': '2026-07-21 14:00:00',
                'source_size': stat.st_size,
                'source_mtime_ns': stat.st_mtime_ns,
            }]
            with mock.patch.object(api, 'CDR_ROOT', root), \
                 mock.patch.object(api.db, 'run_query', return_value=(complete_row, None)):
                self.assertTrue(api.all_entities_days_in_db(
                    ['MyCallConnect'], '2026-07-21', '2026-07-21'))

            changed_row = [dict(complete_row[0], source_size=stat.st_size + 1)]
            with mock.patch.object(api, 'CDR_ROOT', root), \
                 mock.patch.object(api.db, 'run_query', return_value=(changed_row, None)):
                self.assertFalse(api.all_entities_days_in_db(
                    ['MyCallConnect'], '2026-07-21', '2026-07-21'))

    def test_raw_file_list_skips_empty_days_in_a_selected_range(self):
        with tempfile.TemporaryDirectory() as root:
            existing = Path(root, 'Dialphone', '2026', '07', '14', '03.csv.gz')
            existing.parent.mkdir(parents=True)
            existing.write_bytes(b'cdr')
            with mock.patch.object(api, 'CDR_ROOT', root):
                files = api.build_csv_glob(
                    ['Dialphone'], '2026-07-14', '2026-07-15',
                )
            self.assertEqual(len(files), 1)
            self.assertIn('2026/07/14/03.csv.gz', files[0])
            self.assertNotIn('2026/07/15', files[0])


class QueryLogicTests(unittest.TestCase):
    def test_global_sort_filter_and_full_customer_list_are_backend_driven(self):
        totals_row = [{
            'pair_count': 2, 'customer_count': 2, 'code_count': 1,
            'customers': ['A', 'B'], 'attempts': 100, 'completions': 40,
            'minutes': 10, 'revenue': 2, 'cost': 1, 'margin': 1,
        }]
        result_rows = [{
            'customer': 'A', 'code': '212555', 'margin': -1,
            'filtered_row_count': 1,
        }]
        captured_sql = []

        def fake_query(sql, **kwargs):
            captured_sql.append(sql)
            return (totals_row, None) if 'SUM(attempts)' in sql else (result_rows, None)

        body = {
            'start_date': '2026-07-21', 'end_date': '2026-07-21',
            'entities': ['MyCallConnect'], 'sip_codes': [],
            'sort_by': 'margin', 'sort_dir': 'asc',
            'quick_filter': 'fas-suspect', 'limit': 5000,
        }
        with mock.patch.object(api, 'all_entities_days_in_db', return_value=True), \
             mock.patch.object(api.db, 'run_query', side_effect=fake_query):
            result = api.compute_usa_customer_codes(body)

        top_sql = captured_sql[1]
        self.assertIn("COALESCE(orig_trunk_group_name, '(none)') AS customer", top_sql)
        self.assertIn('orig_billed_prefix', top_sql)
        self.assertIn('term_billed_prefix', top_sql)
        self.assertIn("trim(orig_billed_prefix)", top_sql)
        self.assertIn("trim(term_billed_prefix)", top_sql)
        self.assertIn('AS term_code', top_sql)
        self.assertIn('GROUP BY customer, code, term_code', top_sql)
        self.assertNotIn('substring(to_did, 2, 6)', top_sql)
        self.assertIn('WHERE asr_pct < 15 AND attempts >= 50', top_sql)
        self.assertIn('ORDER BY margin ASC', top_sql)
        self.assertNotIn('sip_code IN', top_sql)
        self.assertEqual(result['customers'], ['A', 'B'])
        self.assertEqual(result['total_row_count'], 1)
        self.assertEqual(result['totals']['asr_pct'], 40.0)
        self.assertNotIn('filtered_row_count', result['rows'][0])

    def test_large_export_is_rejected_instead_of_buffered(self):
        body = {
            'start_date': '2026-07-21', 'end_date': '2026-07-21',
            'entities': ['MyCallConnect'], 'sip_codes': [],
        }
        compute = mock.Mock(return_value={
            'rows': [], 'total_row_count': api.CSV_EXPORT_MAX_ROWS + 1,
        })
        with mock.patch.object(api, 'all_entities_days_in_db', return_value=True):
            result, error = api.prepare_export(body, compute)
        self.assertIsNone(result)
        self.assertEqual(error[1], 413)

    def test_streamed_customer_export_can_read_raw_files_without_a_row_limit(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root, 'Dialphone', '2026', '07', '21', '00.csv.gz')
            source.parent.mkdir(parents=True)
            source.write_bytes(b'cdr')
            body = {
                'start_date': '2026-07-21', 'end_date': '2026-07-21',
                'entities': ['Dialphone'], 'sip_codes': [200],
                'sort_by': 'customer', 'sort_dir': 'asc',
            }
            parsed, error = api.validate_body(body)
            self.assertIsNone(error)
            with mock.patch.object(api, 'CDR_ROOT', root):
                sql, error = api.build_customer_codes_export_sql(parsed, use_db=False)
            self.assertIsNone(error)
            self.assertIn('read_csv_auto', sql)
            self.assertIn('AS origin_trunk', sql)
            self.assertIn('orig_billed_prefix', sql)
            self.assertIn('term_billed_prefix', sql)
            self.assertIn('AS term_code', sql)
            self.assertNotIn('substring(to_did, 2, 6)', sql)
            self.assertIn('ORDER BY origin_trunk ASC', sql)
            self.assertNotIn('LIMIT ', sql.upper())

    def test_csv_ticket_is_short_lived_and_contains_validated_filters(self):
        old_token = api.AUTH_TOKEN
        try:
            api.AUTH_TOKEN = 'test-token'
            client = api.app.test_client()
            response = client.post(
                '/api/usa-customer-codes/csv-ticket',
                json={
                    'start_date': '2026-07-21',
                    'end_date': '2026-07-21',
                    'entities': ['MyCallConnect'],
                    'sip_codes': [200],
                },
                headers={'X-Auth-Token': 'test-token'},
            )
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertIn('/api/usa-customer-codes/csv?ticket=', payload['download_url'])
            ticket = payload['download_url'].split('ticket=', 1)[1]
            decoded = api.csv_ticket_serializer().loads(ticket, max_age=600)
            self.assertEqual(decoded['entities'], ['MyCallConnect'])
            self.assertEqual(decoded['sip_codes'], [200])
        finally:
            api.AUTH_TOKEN = old_token


class SecurityAndUiTests(unittest.TestCase):
    def test_raw_sql_is_disabled_by_default(self):
        old_token = api.AUTH_TOKEN
        try:
            api.AUTH_TOKEN = 'test-token'
            client = api.app.test_client()
            response = client.post(
                '/sql', json={'sql': 'SELECT 1'},
                headers={'X-Auth-Token': 'test-token'},
            )
            self.assertEqual(response.status_code, 404)
        finally:
            api.AUTH_TOKEN = old_token

    def test_ui_defaults_to_all_outcomes_and_no_cross_filter_cache_fallback(self):
        html = Path('templates/index.html').read_text(encoding='utf-8')
        js = Path('static/app.js').read_text(encoding='utf-8')
        self.assertIn('class="chip active" data-sip="all"', html)
        self.assertIn("selectedSips: new Set(['all'])", js)
        self.assertNotIn('localCacheGetAny', js)
        self.assertIn("fetch('/api/daily-snapshot'", js)

    def test_daily_snapshot_endpoint_never_computes(self):
        old_token = api.AUTH_TOKEN
        cached = {
            'response': {'rows': [{'code': '212555'}], 'totals': {}},
            'snapshot_date': '2026-07-22',
            'refreshed_at': 1.0,
            'age_seconds': 2.0,
            'tier': 'yesterday',
            'ttl': 900,
            'stale': False,
            'compute_ms': 123,
        }
        try:
            api.AUTH_TOKEN = 'test-token'
            client = api.app.test_client()
            with mock.patch.object(cache, 'latest_daily_snapshot', return_value=cached):
                response = client.get(
                    '/api/daily-snapshot',
                    headers={'X-Auth-Token': 'test-token'},
                )
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload['_snapshot']['date'], '2026-07-22')
            self.assertEqual(payload['rows'][0]['code'], '212555')
        finally:
            api.AUTH_TOKEN = old_token


if __name__ == '__main__':
    unittest.main()
