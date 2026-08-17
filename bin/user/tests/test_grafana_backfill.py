# Copyright 2026 - Distributed under the terms of the GNU Public License (GPLv3)
"""Tests for bin/user/grafana_backfill.py's non-CLI pieces: timestamp
parsing, station/model resolution, and record-to-OpenMetrics-families
collection. These operate on plain in-memory record lists, so no weewx
database is needed.

Run from the extension root:
    PYTHONPATH=/path/to/weewx/src python bin/user/tests/test_grafana_backfill.py
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import grafana_backfill  # noqa: E402

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


if __name__ == '__main__':
    unittest.main()