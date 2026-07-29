import gzip
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import clickhouse_ingester as ingester


VALID_HEADERS = [
    'sip_code',
    'call_start_time',
    'call_end_time',
    'term_media_ip',
    'term_ip',
    'term_carrier_name',
    'term_trunk_group_name',
    'orig_trunk_group_name',
    'orig_billed_duration',
    'orig_cost',
    'term_cost',
    'callid',
]


def write_gzip_csv(path, headers=VALID_HEADERS):
    path.parent.mkdir(parents=True, exist_ok=True)
    values = {
        'sip_code': '200',
        'call_start_time': '2026-07-28 00:00:01',
        'call_end_time': '2026-07-28 00:01:01',
        'term_media_ip': '203.0.113.10',
        'term_ip': '198.51.100.20',
        'term_carrier_name': 'Vendor A',
        'term_trunk_group_name': 'Vendor-A-TG',
        'orig_trunk_group_name': 'Customer A',
        'orig_billed_duration': '60',
        'orig_cost': '0.01',
        'term_cost': '0.005',
        'callid': 'call-1',
    }
    with gzip.open(path, mode='wt', encoding='utf-8', newline='') as stream:
        stream.write(','.join(headers) + '\n')
        stream.write(','.join(values.get(header, '') for header in headers) + '\n')


class HeaderAndDiscoveryTests(unittest.TestCase):
    def test_reads_required_46labs_term_media_ip_header(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp, '00.csv.gz')
            write_gzip_csv(path)
            headers = ingester.read_csv_header(path)
        self.assertIn('term_media_ip', headers)

    def test_rejects_a_file_without_term_media_ip(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp, '00.csv.gz')
            write_gzip_csv(
                path,
                [header for header in VALID_HEADERS if header != 'term_media_ip'],
            )
            with self.assertRaisesRegex(ValueError, 'term_media_ip'):
                ingester.read_csv_header(path)

    def test_discovers_only_stable_hourly_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            stable = root / 'MyCallConnect' / '2026' / '07' / '28' / '00.csv.gz'
            active = root / 'MyCallConnect' / '2026' / '07' / '28' / '01.csv.gz'
            ignored = root / 'MyCallConnect' / '2026' / '07' / '28' / 'notes.csv.gz'
            for path in (stable, active, ignored):
                write_gzip_csv(path)
            now = datetime(2026, 7, 28, 2, 0, tzinfo=timezone.utc)
            os.utime(stable, (now.timestamp() - 3600, now.timestamp() - 3600))
            os.utime(active, (now.timestamp() - 10, now.timestamp() - 10))
            os.utime(ignored, (now.timestamp() - 3600, now.timestamp() - 3600))
            with mock.patch.object(ingester, 'MIN_AGE_SECONDS', 600):
                files = ingester.discover_files(
                    root=root, lookback_days=1, now=now,
                )
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].path.name, '00.csv.gz')
        self.assertEqual(
            files[0].clickhouse_path,
            'cdr/MyCallConnect/2026/07/28/00.csv.gz',
        )


class SqlAndIdempotencyTests(unittest.TestCase):
    def source(self):
        return ingester.SourceFile(
            entity='MyCallConnect',
            file_hour=datetime(2026, 7, 28, 0, tzinfo=timezone.utc),
            path=Path('/data/raw/MyCallConnect/2026/07/28/00.csv.gz'),
            clickhouse_path='cdr/MyCallConnect/2026/07/28/00.csv.gz',
            size=123,
            mtime_ns=456,
        )

    def test_insert_sql_maps_term_media_ip_and_cost_fields(self):
        sql = ingester.build_insert_sql(self.source(), VALID_HEADERS)
        self.assertIn('term_media_ip', sql)
        self.assertIn('`term_media_ip`', sql)
        self.assertIn('toFloat64OrZero(`term_cost`)', sql)
        self.assertIn("FROM file('cdr/MyCallConnect/2026/07/28/00.csv.gz'", sql)
        self.assertNotIn('ignore_errors', sql.lower())

    def test_completed_matching_file_is_never_inserted_twice(self):
        previous = {
            'source_size': 123,
            'source_mtime_ns': 456,
            'rows_ingested': 99,
            'status': 'done',
            'message': 'complete',
        }
        with mock.patch.object(ingester, 'latest_log', return_value=previous), \
             mock.patch.object(ingester, 'read_csv_header') as read_header:
            status, rows = ingester.ingest_one(mock.Mock(), self.source())
        self.assertEqual((status, rows), ('skipped', 99))
        read_header.assert_not_called()

    def test_changed_completed_file_is_blocked_and_flagged(self):
        previous = {
            'source_size': 122,
            'source_mtime_ns': 455,
            'rows_ingested': 99,
            'status': 'done',
            'message': 'complete',
        }
        with mock.patch.object(ingester, 'latest_log', return_value=previous), \
             mock.patch.object(ingester, 'write_log') as write_log:
            status, rows = ingester.ingest_one(mock.Mock(), self.source())
        self.assertEqual((status, rows), ('changed', 99))
        self.assertEqual(write_log.call_args.args[2], 'changed')

    def test_a_changed_file_cannot_be_inserted_after_changing_again(self):
        previous = {
            'source_size': 122,
            'source_mtime_ns': 455,
            'rows_ingested': 99,
            'status': 'changed',
            'message': 'already changed',
        }
        with mock.patch.object(ingester, 'latest_log', return_value=previous), \
             mock.patch.object(ingester, 'write_log') as write_log, \
             mock.patch.object(ingester, 'read_csv_header') as read_header:
            status, rows = ingester.ingest_one(mock.Mock(), self.source())
        self.assertEqual((status, rows), ('changed', 99))
        self.assertEqual(write_log.call_args.args[2], 'changed')
        read_header.assert_not_called()


class ProvisioningTests(unittest.TestCase):
    def test_schema_and_dashboard_are_term_media_ip_first(self):
        schema = Path('clickhouse/init/001_schema.sql').read_text(encoding='utf-8')
        dashboard = json.loads(
            Path('grafana/dashboards/term-media-ip.json').read_text(
                encoding='utf-8',
            )
        )
        dashboard_sql = '\n'.join(
            target.get('rawSql', '')
            for panel in dashboard['panels']
            for target in panel.get('targets', [])
        )
        self.assertIn('term_media_ip String', schema)
        self.assertIn('cdr_hourly_media_ip_mv', schema)
        self.assertIn('term_media_ip', dashboard_sql)
        self.assertIn(
            '$__conditionalAll('
            'term_media_ip = ${media_ip:singlequote}, $media_ip)',
            dashboard_sql,
        )
        self.assertNotIn(':sqlstring}', dashboard_sql)
        self.assertNotIn("singlequote} = 'All'", dashboard_sql)
        self.assertIn('cdr.ingest_log FINAL', dashboard_sql)
        self.assertEqual(dashboard['uid'], 'cdr-term-media-ip')
        self.assertEqual(dashboard['title'], 'CDR - Simple Route Analytics')
        self.assertNotIn('groupUniqArray', dashboard_sql)
        self.assertNotIn('any(term_ip)', dashboard_sql)
        self.assertIn(
            'GROUP BY orig_trunk_group_name, term_carrier_name, '
            'term_trunk_group_name, term_ip, term_media_ip',
            dashboard_sql,
        )
        self.assertEqual(
            [variable['name'] for variable in dashboard['templating']['list']],
            ['entity', 'origin_trunk', 'vendor', 'trunk', 'media_ip'],
        )
        for variable in dashboard['templating']['list']:
            self.assertIsNone(variable['allValue'])
            self.assertEqual(variable['current']['value'], '$__all')
        route_panels = [
            panel for panel in dashboard['panels']
            if panel.get('datasource', {}).get('uid') == 'cdr-clickhouse'
            and panel['id'] != 15
        ]
        self.assertTrue(route_panels)
        for panel in route_panels:
            panel_sql = panel['targets'][0]['rawSql']
            self.assertIn('$origin_trunk', panel_sql, panel['title'])

    def test_dashboard_provisioning_is_outside_persistent_grafana_volume(self):
        dockerfile = Path('Dockerfile.grafana').read_text(encoding='utf-8')
        provider = Path(
            'grafana/provisioning/dashboards/provider.yml',
        ).read_text(encoding='utf-8')
        self.assertIn(
            'COPY grafana/dashboards/ /etc/grafana/dashboards/',
            dockerfile,
        )
        self.assertIn('path: /etc/grafana/dashboards', provider)
        self.assertNotIn('path: /var/lib/grafana/dashboards', provider)


if __name__ == '__main__':
    unittest.main()
