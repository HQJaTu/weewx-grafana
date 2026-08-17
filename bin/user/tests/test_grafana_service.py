# Copyright 2026 - Distributed under the terms of the GNU Public License (GPLv3)
"""Broker/network-free tests for bin/user/grafana.py's GrafanaThread.

These patch urllib.request.urlopen (see fakes.FakeOpener) so the full HTTP
posting path -- including weewx.restx.RESTThread's built-in retry logic --
can be exercised without a live Grafana Cloud endpoint.

Run from the extension root:
    PYTHONPATH=/path/to/weewx/src python bin/user/tests/test_grafana_service.py
"""
import base64
import json
import os
import sys
import unittest
import urllib.error
import urllib.request

try:
    import queue as Queue
except ImportError:
    import Queue

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import grafana  # noqa: E402
from fakes import FakeOpener, FakeResponse  # noqa: E402

import weewx  # noqa: E402
import weewx.restx  # noqa: E402


RECORD = {
    'dateTime': 1700000000, 'usUnits': weewx.US, 'interval': 5,
    'outTemp': 61.0, 'outHumidity': 78.0, 'barometer': 29.95,
}

EMPTY_RECORD = {'dateTime': 1700000000, 'usUnits': weewx.US, 'interval': 5}

NO_USUNITS_RECORD = {'dateTime': 1700000000, 'interval': 5, 'outTemp': 61.0}


def make_thread(**kwargs):
    opts = dict(otlp_endpoint='https://otlp-gateway.example/otlp/v1/metrics',
                instance_id='123456', api_key='secret-token',
                station='TestStation', model='Simulator',
                retry_wait=0, timeout=1)
    opts.update(kwargs)
    return grafana.GrafanaThread(Queue.Queue(), **opts)


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


class AuthHeaderTest(unittest.TestCase):

    def test_basic_auth_header_is_correct(self):
        t = make_thread(instance_id='42', api_key='hunter2')
        expected = 'Basic ' + base64.b64encode(b'42:hunter2').decode('ascii')
        self.assertEqual(t._auth_header, expected)

    def test_request_carries_auth_header(self):
        t = make_thread()
        request = t.get_request(t.otlp_endpoint)
        self.assertEqual(request.get_header('Authorization'), t._auth_header)


class FormatUrlTest(unittest.TestCase):

    def test_format_url_returns_configured_endpoint(self):
        t = make_thread(otlp_endpoint='https://example/otlp/v1/metrics')
        self.assertEqual(t.format_url(RECORD), 'https://example/otlp/v1/metrics')


class GetRecordTest(unittest.TestCase):

    def test_missing_usunits_aborts(self):
        t = make_thread()
        with self.assertRaises(weewx.restx.AbortedPost):
            t.get_record(dict(NO_USUNITS_RECORD), None)

    def test_no_manager_returns_record_unchanged(self):
        t = make_thread()
        result = t.get_record(dict(RECORD), None)
        self.assertEqual(result['outTemp'], 61.0)
        self.assertEqual(result['usUnits'], weewx.US)

    def test_unit_system_conversion(self):
        t = make_thread(unit_system=weewx.METRIC)
        result = t.get_record(dict(RECORD), None)
        self.assertEqual(result['usUnits'], weewx.METRIC)
        self.assertAlmostEqual(result['outTemp'], (61.0 - 32) * 5.0 / 9.0, places=6)


class GetPostBodyTest(unittest.TestCase):

    def test_body_contains_expected_metric(self):
        t = make_thread()
        body, content_type = t.get_post_body(dict(RECORD))
        self.assertEqual(content_type, 'application/json')
        payload = json.loads(body)
        names = [m['name'] for m in
                 payload['resourceMetrics'][0]['scopeMetrics'][0]['metrics']]
        self.assertIn('weewx_outdoor_temperature_fahrenheit', names)

    def test_labels_reflect_station_and_model(self):
        t = make_thread(station='Home', model='VantagePro2')
        body, _ct = t.get_post_body(dict(RECORD))
        payload = json.loads(body)
        attrs = {a['key']: a['value']['stringValue']
                 for a in payload['resourceMetrics'][0]['resource']['attributes']}
        self.assertEqual(attrs['station'], 'Home')
        self.assertEqual(attrs['model'], 'VantagePro2')
        self.assertEqual(attrs['unit_system'], 'US')

    def test_no_observations_aborts(self):
        t = make_thread()
        with self.assertRaises(weewx.restx.AbortedPost):
            t.get_post_body(dict(EMPTY_RECORD))


class CheckResponseTest(unittest.TestCase):

    def test_clean_response_passes(self):
        t = make_thread()
        t.check_response(FakeResponse(200, b'{}'))  # must not raise

    def test_empty_body_passes(self):
        t = make_thread()
        t.check_response(FakeResponse(200, b''))  # must not raise

    def test_partial_success_with_rejections_fails(self):
        t = make_thread()
        body = json.dumps({'partialSuccess': {
            'rejectedDataPoints': 2, 'errorMessage': 'bad data point'}}).encode('utf-8')
        with self.assertRaises(weewx.restx.FailedPost) as cm:
            t.check_response(FakeResponse(200, body))
        self.assertIn('2', str(cm.exception))

    def test_partial_success_with_zero_rejections_passes(self):
        t = make_thread()
        body = json.dumps({'partialSuccess': {'rejectedDataPoints': 0}}).encode('utf-8')
        t.check_response(FakeResponse(200, body))  # must not raise


class ProcessRecordTest(unittest.TestCase):

    def test_successful_post(self):
        t = make_thread()
        with OpenerPatch() as opener:
            t.process_record(dict(RECORD), None)
        self.assertEqual(len(opener.calls), 1)
        request, data, _timeout = opener.calls[0]
        self.assertEqual(request.get_full_url(), t.otlp_endpoint)
        self.assertEqual(request.get_header('Authorization'), t._auth_header)
        self.assertEqual(request.get_header('Content-type'), 'application/json')
        payload = json.loads(data)
        names = [m['name'] for m in
                 payload['resourceMetrics'][0]['scopeMetrics'][0]['metrics']]
        self.assertIn('weewx_outdoor_temperature_fahrenheit', names)

    def test_skip_upload_makes_no_request(self):
        t = make_thread(skip_upload=True)
        with OpenerPatch() as opener:
            with self.assertRaises(weewx.restx.AbortedPost):
                t.process_record(dict(RECORD), None)
        self.assertEqual(opener.calls, [])

    def test_no_observations_makes_no_request(self):
        t = make_thread()
        with OpenerPatch() as opener:
            with self.assertRaises(weewx.restx.AbortedPost):
                t.process_record(dict(EMPTY_RECORD), None)
        self.assertEqual(opener.calls, [])

    def test_server_error_is_retried_then_raises(self):
        t = make_thread(max_tries=2)
        with OpenerPatch() as opener:
            opener.queue_response(FakeResponse(500, b'server error'))
            opener.queue_response(FakeResponse(500, b'server error'))
            with self.assertRaises(weewx.restx.FailedPost):
                t.process_record(dict(RECORD), None)
        self.assertEqual(len(opener.calls), 2)

    def test_connection_error_is_retried_then_raises(self):
        t = make_thread(max_tries=2)
        with OpenerPatch() as opener:
            opener.queue_response(urllib.error.URLError('connection refused'))
            opener.queue_response(urllib.error.URLError('connection refused'))
            with self.assertRaises(weewx.restx.FailedPost):
                t.process_record(dict(RECORD), None)
        self.assertEqual(len(opener.calls), 2)


class ConfigParsingTest(unittest.TestCase):

    def test_parse_inputs_normalizes_units_alias(self):
        raw = {'outTemp': {'units': 'degree_F'}}
        parsed = grafana._parse_inputs(raw)
        self.assertEqual(parsed['outTemp'], {'unit': 'degree_F'})

    def test_parse_inputs_leaves_unit_alone(self):
        raw = {'outTemp': {'unit': 'degree_C', 'name': 'custom'}}
        parsed = grafana._parse_inputs(raw)
        self.assertEqual(parsed['outTemp'], {'unit': 'degree_C', 'name': 'custom'})

    def test_parse_skip_fields_from_single_string(self):
        self.assertEqual(grafana._parse_skip_fields('windSpeed'), {'windSpeed'})

    def test_parse_skip_fields_from_list(self):
        self.assertEqual(grafana._parse_skip_fields(['a', 'b']), {'a', 'b'})

    def test_parse_skip_fields_from_none(self):
        self.assertEqual(grafana._parse_skip_fields(None), set())


if __name__ == '__main__':
    unittest.main()