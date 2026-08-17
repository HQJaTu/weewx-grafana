# Copyright 2026 - Distributed under the terms of the GNU Public License (GPLv3)
"""Tests for bin/user/grafana_metrics.py: naming, unit handling, OTLP payload
construction, and the OpenMetrics writer used by the backfill converter.

Run from the extension root:
    PYTHONPATH=/path/to/weewx/src python bin/user/tests/test_grafana_metrics.py
"""
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import grafana_metrics  # noqa: E402

import weewx  # noqa: E402


US_RECORD = {
    'dateTime': 1700000000, 'usUnits': weewx.US, 'interval': 5,
    'outTemp': 61.0, 'outHumidity': 78.0, 'barometer': 29.95,
    'windSpeed': 2.0, 'windDir': 90.0, 'rain': 0.0,
    'extraTemp1': 55.5, 'txBatteryStatus': None, 'consBatteryVoltage': 4.5,
}

METRIC_RECORD = {
    'dateTime': 1700000000, 'usUnits': weewx.METRIC, 'interval': 5,
    'outTemp': 16.1, 'barometer': 1014.2,
}


class MetricNameTest(unittest.TestCase):

    def test_known_observation_and_unit(self):
        self.assertEqual(
            grafana_metrics.metric_name_for('outTemp', 'degree_C'),
            'weewx_outdoor_temperature_celsius')

    def test_same_observation_different_unit(self):
        self.assertEqual(
            grafana_metrics.metric_name_for('outTemp', 'degree_F'),
            'weewx_outdoor_temperature_fahrenheit')

    def test_unmapped_observation_falls_back_to_snake_case(self):
        self.assertEqual(
            grafana_metrics.metric_name_for('extraTemp1', 'degree_C'),
            'weewx_extra_temp_1_celsius')

    def test_unmapped_unit_omits_suffix(self):
        # 'NONE' (dimensionless / unknown) unit types contribute no suffix.
        self.assertEqual(
            grafana_metrics.metric_name_for('txBatteryStatus', 'NONE'),
            'weewx_tx_battery_status')

    def test_name_override_used_verbatim(self):
        self.assertEqual(
            grafana_metrics.metric_name_for('barometer', 'hPa', name_override='custom_name'),
            'custom_name')


class IterObservationsTest(unittest.TestCase):

    def test_basic_fields_present(self):
        obs = dict((name, (unit, value)) for name, unit, value in
                   grafana_metrics.iter_observations(US_RECORD))
        self.assertIn('weewx_outdoor_temperature_fahrenheit', obs)
        self.assertEqual(obs['weewx_outdoor_temperature_fahrenheit'], ('degree_F', 61.0))
        self.assertIn('weewx_barometric_pressure_inches_mercury', obs)

    def test_meta_fields_excluded(self):
        names = [n for n, _u, _v in grafana_metrics.iter_observations(US_RECORD)]
        self.assertNotIn('weewx_date_time', names)
        for excluded in ('usUnits', 'interval', 'dateTime'):
            self.assertTrue(all(excluded not in n for n in names))

    def test_none_values_skipped(self):
        names = [n for n, _u, _v in grafana_metrics.iter_observations(US_RECORD)]
        self.assertNotIn('weewx_tx_battery_status', names)

    def test_non_numeric_values_skipped(self):
        record = dict(US_RECORD, someString='not a number')
        names = [n for n, _u, _v in grafana_metrics.iter_observations(record)]
        self.assertTrue(all('some_string' not in n for n in names))

    def test_skip_fields_option(self):
        names = [n for n, _u, _v in
                  grafana_metrics.iter_observations(US_RECORD, skip_fields={'windSpeed'})]
        self.assertTrue(all('wind_speed' not in n for n in names))

    def test_inputs_unit_override_converts_value(self):
        inputs = {'outTemp': {'unit': 'degree_C'}}
        obs = dict((name, (unit, value)) for name, unit, value in
                   grafana_metrics.iter_observations(US_RECORD, inputs=inputs))
        self.assertIn('weewx_outdoor_temperature_celsius', obs)
        unit_type, value = obs['weewx_outdoor_temperature_celsius']
        self.assertEqual(unit_type, 'degree_C')
        self.assertAlmostEqual(value, (61.0 - 32) * 5.0 / 9.0, places=6)

    def test_inputs_name_override_replaces_name_entirely(self):
        inputs = {'barometer': {'name': 'my_custom_pressure'}}
        names = [n for n, _u, _v in grafana_metrics.iter_observations(US_RECORD, inputs=inputs)]
        self.assertIn('my_custom_pressure', names)
        self.assertNotIn('weewx_barometric_pressure_inches_mercury', names)


class OtlpPayloadTest(unittest.TestCase):

    def test_resource_attributes(self):
        payload = grafana_metrics.build_otlp_payload(US_RECORD, 'Home', 'VantagePro2', 'US')
        attrs = {a['key']: a['value']['stringValue']
                 for a in payload['resourceMetrics'][0]['resource']['attributes']}
        self.assertEqual(attrs, {'station': 'Home', 'model': 'VantagePro2', 'unit_system': 'US'})

    def test_metric_shape_and_timestamp(self):
        payload = grafana_metrics.build_otlp_payload(US_RECORD, 'Home', 'VantagePro2', 'US')
        metrics = payload['resourceMetrics'][0]['scopeMetrics'][0]['metrics']
        by_name = {m['name']: m for m in metrics}
        m = by_name['weewx_outdoor_temperature_fahrenheit']
        dp = m['gauge']['dataPoints'][0]
        self.assertEqual(dp['asDouble'], 61.0)
        self.assertEqual(dp['timeUnixNano'], '1700000000000000000')

    def test_unit_symbol_present_for_known_units(self):
        payload = grafana_metrics.build_otlp_payload(METRIC_RECORD, 'Home', 'VantagePro2', 'METRIC')
        metrics = payload['resourceMetrics'][0]['scopeMetrics'][0]['metrics']
        by_name = {m['name']: m for m in metrics}
        self.assertEqual(by_name['weewx_outdoor_temperature_celsius']['unit'], 'Cel')

    def test_empty_record_yields_no_metrics(self):
        record = {'dateTime': 1700000000, 'usUnits': weewx.US, 'interval': 5}
        payload = grafana_metrics.build_otlp_payload(record, 'Home', 'VantagePro2', 'US')
        self.assertEqual(payload['resourceMetrics'][0]['scopeMetrics'][0]['metrics'], [])


class UnitSystemNameTest(unittest.TestCase):

    def test_known_constants(self):
        self.assertEqual(grafana_metrics.unit_system_name(weewx.US), 'US')
        self.assertEqual(grafana_metrics.unit_system_name(weewx.METRIC), 'METRIC')
        self.assertEqual(grafana_metrics.unit_system_name(weewx.METRICWX), 'METRICWX')


class OpenMetricsWriterTest(unittest.TestCase):

    def test_type_and_eof_markers(self):
        families = {
            'weewx_outdoor_temperature_celsius': {
                'samples': [({'station': 'Home'}, 1700000000, 21.5)],
            },
        }
        buf = io.StringIO()
        grafana_metrics.write_openmetrics(buf, families)
        text = buf.getvalue()
        self.assertIn('# TYPE weewx_outdoor_temperature_celsius gauge', text)
        self.assertTrue(text.strip().endswith('# EOF'))
        self.assertIn('weewx_outdoor_temperature_celsius{station="Home"} 21.5 1700000000', text)

    def test_samples_within_a_family_are_contiguous_and_sorted_by_time(self):
        families = {
            'weewx_outdoor_temperature_celsius': {
                'samples': [
                    ({'station': 'Home'}, 1700000010, 22.0),
                    ({'station': 'Home'}, 1700000000, 21.5),
                ],
            },
        }
        buf = io.StringIO()
        grafana_metrics.write_openmetrics(buf, families)
        lines = [l for l in buf.getvalue().splitlines() if l.startswith('weewx_')]
        self.assertEqual(len(lines), 2)
        self.assertIn('1700000000', lines[0])
        self.assertIn('1700000010', lines[1])

    def test_label_value_escaping(self):
        families = {
            'weewx_test_metric': {
                'samples': [({'station': 'a "quoted" \\ name'}, 1700000000, 1.0)],
            },
        }
        buf = io.StringIO()
        grafana_metrics.write_openmetrics(buf, families)
        self.assertIn(r'station="a \"quoted\" \\ name"', buf.getvalue())

    def test_multiple_families_each_own_type_block(self):
        families = {
            'weewx_a': {'samples': [({}, 1700000000, 1.0)]},
            'weewx_b': {'samples': [({}, 1700000000, 2.0)]},
        }
        buf = io.StringIO()
        grafana_metrics.write_openmetrics(buf, families)
        text = buf.getvalue()
        self.assertEqual(text.count('# TYPE'), 2)
        self.assertEqual(text.count('# EOF'), 1)


if __name__ == '__main__':
    unittest.main()