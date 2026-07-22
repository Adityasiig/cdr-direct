import os
import tempfile
import unittest
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


if __name__ == '__main__':
    unittest.main()
