# Copyright 2026 - Distributed under the terms of the GNU Public License (GPLv3)
"""Tests for bin/user/grafana_backfill.py's non-CLI pieces: timestamp
parsing, station/model resolution, and record-to-OpenMetrics-families
collection. These operate on plain in-memory record lists, so no weewx
database is needed.

Run from the extension root:
    PYTHONPATH=/path/to/weewx/src python bin/user/tests/test_grafana_backfill.py
"""
import base64
import io
import os
import sys
import time
import unittest
import urllib.error
import urllib.request

import cramjam

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import grafana_backfill  # noqa: E402
import grafana_metrics  # noqa: E402
import remote_write_pb2  # noqa: E402
from fakes import FakeOpener, FakeResponse  # noqa: E402

import weewx  # noqa: E402


RECORDS = [
    {'dateTime': 1700000000, 'usUnits': weewx.US, 'interval': 5, 'outTemp': 61.0},
    {'dateTime': 1700000300, 'usUnits': weewx.US, 'interval': 5, 'outTemp': 63.0},
    {'dateTime': 1700000600, 'usUnits': weewx.US, 'interval': 5, 'outTemp': None},
    {'dateTime': 1700000900, 'interval': 5, 'outTemp': 64.0},  # no usUnits: skipped
]


class ParseTimestampTest(unittest.TestCase):

    def test_epoch_integer(self):
        self.assertEqual(grafana_backfill.parse_timestamp('1700000000'), 1700000000)

    def test_date_only(self):
        expected = int(time.mktime(time.strptime('2023-11-14', '%Y-%m-%d')))
        self.assertEqual(grafana_backfill.parse_timestamp('2023-11-14'), expected)

    def test_date_and_time(self):
        expected = int(time.mktime(time.strptime('2023-11-14 08:30:00', '%Y-%m-%d %H:%M:%S')))
        self.assertEqual(grafana_backfill.parse_timestamp('2023-11-14 08:30:00'), expected)

    def test_invalid_raises(self):
        with self.assertRaises(ValueError):
            grafana_backfill.parse_timestamp('not-a-date')


class CreateParserTest(unittest.TestCase):
    """Guards against a renamed --flag whose dest no longer matches the
    args.* attribute main() reads (argparse won't catch that itself)."""

    def test_parsed_args_have_the_attributes_main_reads(self):
        args = grafana_backfill.create_parser().parse_args([
            '--weewx-config', '/etc/weewx/weewx.conf',
            '--until', '2023-11-14',
            '--output-file', 'out.prom',
        ])
        self.assertEqual(args.weewx_config, '/etc/weewx/weewx.conf')
        self.assertEqual(args.output_file, 'out.prom')


class ResolveStationAndModelTest(unittest.TestCase):

    def test_cli_args_take_precedence(self):
        config = {'StdRESTful': {'GrafanaCloud': {'station': 'FromConfig'}}}
        station, _model = grafana_backfill.resolve_station_and_model(
            config, station_arg='FromCli')
        self.assertEqual(station, 'FromCli')

    def test_falls_back_to_grafanacloud_section(self):
        config = {'StdRESTful': {'GrafanaCloud': {'station': 'Configured', 'model': 'VP2'}}}
        station, model = grafana_backfill.resolve_station_and_model(config)
        self.assertEqual(station, 'Configured')
        self.assertEqual(model, 'VP2')

    def test_falls_back_to_station_section(self):
        config = {'Station': {'location': 'My Backyard', 'station_type': 'Vantage'}}
        station, model = grafana_backfill.resolve_station_and_model(config)
        self.assertEqual(station, 'My Backyard')
        self.assertEqual(model, 'Vantage')

    def test_default_when_nothing_configured(self):
        station, model = grafana_backfill.resolve_station_and_model({})
        self.assertEqual(station, 'weewx')
        self.assertEqual(model, 'weewx')

    def test_comma_containing_value_parsed_as_list_is_rejoined(self):
        # configobj parses an unquoted comma-containing value (e.g.
        # 'station = Ojala, Lappeenranta') into a list rather than a string;
        # left as a list it isn't hashable and breaks build_write_request().
        config = {'StdRESTful': {'GrafanaCloud': {
            'station': ['Ojala', 'Lappeenranta']}}}
        station, _model = grafana_backfill.resolve_station_and_model(config)
        self.assertEqual(station, 'Ojala, Lappeenranta')


class CollectFamiliesTest(unittest.TestCase):

    def test_groups_samples_under_one_family(self):
        families, count = grafana_backfill.collect_families(RECORDS, 'Home', 'VP2')
        # 3 records have usUnits (and so count); the 4th has none and is skipped
        # entirely. Only 2 of those 3 contribute a sample (outTemp is None on one).
        self.assertEqual(count, 3)
        name = 'weewx_outdoor_temperature_fahrenheit'
        self.assertIn(name, families)
        samples = families[name]['samples']
        self.assertEqual(len(samples), 2)
        values = sorted(v for _labels, _ts, v in samples)
        self.assertEqual(values, [61.0, 63.0])

    def test_labels_on_every_sample(self):
        families, _count = grafana_backfill.collect_families(RECORDS, 'Home', 'VP2')
        name = 'weewx_outdoor_temperature_fahrenheit'
        for labels, _ts, _v in families[name]['samples']:
            self.assertEqual(labels, {'station': 'Home', 'model': 'VP2', 'unit_system': 'US'})

    def test_records_without_usunits_are_skipped(self):
        # 4 records total; the last has no usUnits and must not contribute
        # any sample (nor should the one with a None outTemp value).
        families, _count = grafana_backfill.collect_families(RECORDS, 'Home', 'VP2')
        total_samples = sum(len(f['samples']) for f in families.values())
        self.assertEqual(total_samples, 2)

    def test_unit_system_override_converts_and_relabels(self):
        families, _count = grafana_backfill.collect_families(
            RECORDS, 'Home', 'VP2', unit_system_override=weewx.METRIC)
        name = 'weewx_outdoor_temperature_celsius'
        self.assertIn(name, families)
        labels, _ts, _v = families[name]['samples'][0]
        self.assertEqual(labels['unit_system'], 'METRIC')


class IterFamilyBatchesTest(unittest.TestCase):

    def test_flushes_a_batch_once_it_reaches_batch_size(self):
        batches = list(grafana_backfill.iter_family_batches(
            RECORDS, 'Home', 'VP2', batch_size=1))
        # Only 2 of the 4 RECORDS contribute an actual sample (outTemp 61.0
        # and 63.0); each flushes its own batch immediately at batch_size=1.
        self.assertEqual(len(batches), 2)
        for _families, count in batches:
            self.assertEqual(count, 1)

    def test_final_partial_batch_is_still_yielded(self):
        batches = list(grafana_backfill.iter_family_batches(
            RECORDS, 'Home', 'VP2', batch_size=100))
        self.assertEqual(len(batches), 1)
        _families, count = batches[0]
        self.assertEqual(count, 3)

    def test_batches_preserve_chronological_order(self):
        batches = list(grafana_backfill.iter_family_batches(
            RECORDS, 'Home', 'VP2', batch_size=1))
        name = 'weewx_outdoor_temperature_fahrenheit'
        timestamps = [ts for families, _count in batches
                      for _labels, ts, _v in families[name]['samples']]
        self.assertEqual(timestamps, sorted(timestamps))


class ResolvePushConfigTest(unittest.TestCase):

    def test_cli_args_take_precedence(self):
        config = {'StdRESTful': {'GrafanaCloud': {
            'prometheus_url': 'https://from-config/push',
            'instance_id': 'from-config-id',
            'api_key': 'from-config-key',
        }}}
        url, instance_id, api_key = grafana_backfill.resolve_push_config(
            config, prometheus_url_arg='https://from-cli/push')
        self.assertEqual(url, 'https://from-cli/push')
        self.assertEqual(instance_id, 'from-config-id')
        self.assertEqual(api_key, 'from-config-key')

    def test_falls_back_to_grafanacloud_section(self):
        config = {'StdRESTful': {'GrafanaCloud': {
            'prometheus_url': 'https://from-config/push',
            'instance_id': '12345',
            'api_key': 'secret',
        }}}
        url, instance_id, api_key = grafana_backfill.resolve_push_config(config)
        self.assertEqual(url, 'https://from-config/push')
        self.assertEqual(instance_id, '12345')
        self.assertEqual(api_key, 'secret')

    def test_missing_config_returns_none(self):
        url, instance_id, api_key = grafana_backfill.resolve_push_config({})
        self.assertIsNone(url)
        self.assertIsNone(instance_id)
        self.assertIsNone(api_key)


class BuildAuthHeaderTest(unittest.TestCase):

    def test_basic_auth_header_is_correct(self):
        header = grafana_backfill.build_auth_header('42', 'hunter2')
        expected = 'Basic ' + base64.b64encode(b'42:hunter2').decode('ascii')
        self.assertEqual(header, expected)


class OpenerPatch:
    """Context manager that patches urllib.request.urlopen for the duration
    of a test, so no real network call is ever made."""

    def __enter__(self):
        self.opener = FakeOpener()
        self._orig = urllib.request.urlopen
        urllib.request.urlopen = self.opener
        return self.opener

    def __exit__(self, *exc_info):
        urllib.request.urlopen = self._orig


def _http_error(code, body=b'error detail'):
    return urllib.error.HTTPError(
        'https://push.example/api/prom/push', code, 'error', {}, io.BytesIO(body))


class PushWriteRequestTest(unittest.TestCase):

    def _write_request(self):
        families = {
            'weewx_outdoor_temperature_celsius': {
                'samples': [({'station': 'Home'}, 1700000000, 21.5)],
            },
        }
        return grafana_metrics.build_write_request(families)

    def test_successful_push_sends_expected_headers_and_body(self):
        write_request = self._write_request()
        with OpenerPatch() as opener:
            grafana_backfill.push_write_request(
                write_request, 'https://push.example/api/prom/push', 'Basic dGVzdA==')
        self.assertEqual(len(opener.calls), 1)
        request, _data, _timeout = opener.calls[0]
        self.assertEqual(request.get_full_url(), 'https://push.example/api/prom/push')
        self.assertEqual(request.get_method(), 'POST')
        self.assertEqual(request.get_header('Content-type'), 'application/x-protobuf')
        self.assertEqual(request.get_header('Content-encoding'), 'snappy')
        self.assertEqual(
            request.get_header('X-prometheus-remote-write-version'), '0.1.0')
        self.assertEqual(request.get_header('Authorization'), 'Basic dGVzdA==')
        decompressed = bytes(cramjam.snappy.decompress_raw(request.data))
        roundtripped = remote_write_pb2.WriteRequest()
        roundtripped.ParseFromString(decompressed)
        self.assertEqual(roundtripped, write_request)

    def test_server_error_is_retried_then_raises(self):
        write_request = self._write_request()
        with OpenerPatch() as opener:
            opener.queue_response(_http_error(500))
            opener.queue_response(_http_error(500))
            with self.assertRaises(RuntimeError):
                grafana_backfill.push_write_request(
                    write_request, 'https://push.example/api/prom/push', 'Basic x',
                    max_tries=2, retry_wait=0)
        self.assertEqual(len(opener.calls), 2)

    def test_too_many_requests_is_retried(self):
        write_request = self._write_request()
        with OpenerPatch() as opener:
            opener.queue_response(_http_error(429))
            opener.queue_response(FakeResponse(200, b''))
            grafana_backfill.push_write_request(
                write_request, 'https://push.example/api/prom/push', 'Basic x',
                max_tries=3, retry_wait=0)
        self.assertEqual(len(opener.calls), 2)

    def test_client_error_raises_immediately_without_retry(self):
        write_request = self._write_request()
        with OpenerPatch() as opener:
            opener.queue_response(_http_error(400, b'bad request'))
            with self.assertRaises(RuntimeError):
                grafana_backfill.push_write_request(
                    write_request, 'https://push.example/api/prom/push', 'Basic x',
                    max_tries=3, retry_wait=0)
        self.assertEqual(len(opener.calls), 1)

    def test_out_of_order_rejection_raises_an_actionable_error_without_retry(self):
        write_request = self._write_request()
        body = (b"received a sample whose timestamp is older than the "
                b"out-of-order time window, timestamp: 1563802200000 series: "
                b"'weewx_barometric_pressure_inches_mercury' "
                b"(err-mimir-sample-timestamp-too-old)")
        with OpenerPatch() as opener:
            opener.queue_response(_http_error(400, body))
            with self.assertRaises(RuntimeError) as cm:
                grafana_backfill.push_write_request(
                    write_request, 'https://push.example/api/prom/push', 'Basic x',
                    max_tries=3, retry_wait=0)
        self.assertEqual(len(opener.calls), 1)
        self.assertIn('--output-file', str(cm.exception))
        self.assertIn('err-mimir-sample-timestamp-too-old', str(cm.exception))

    def test_connection_error_is_retried_then_raises(self):
        write_request = self._write_request()
        with OpenerPatch() as opener:
            opener.queue_response(urllib.error.URLError('connection refused'))
            opener.queue_response(urllib.error.URLError('connection refused'))
            with self.assertRaises(RuntimeError):
                grafana_backfill.push_write_request(
                    write_request, 'https://push.example/api/prom/push', 'Basic x',
                    max_tries=2, retry_wait=0)
        self.assertEqual(len(opener.calls), 2)


if __name__ == '__main__':
    unittest.main()