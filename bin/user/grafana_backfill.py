#!/usr/bin/env python3
# Copyright 2026 - Distributed under the terms of the GNU Public License (GPLv3)
"""Extract historical weewx archive data into OpenMetrics format, for a
one-time backfill of Grafana Cloud with data that predates the live
bin/user/grafana.py uploader.

This tool does not talk to Grafana Cloud itself. It writes a file in
OpenMetrics text exposition format, using exactly the same metric naming and
labels as the live uploader (see bin/user/grafana_metrics.py), so the
backfilled series and the live series are the same series in Grafana Cloud.
That file is then converted to TSDB blocks and uploaded with the standard
Prometheus/Mimir tools:

    promtool tsdb create-blocks-from openmetrics weewx_backfill.prom ./blocks
    mimirtool backfill --address=<mimir-url> --id=<tenant-id> ./blocks/*

Usage:

    PYTHONPATH=/path/to/weewx/src python3 bin/user/grafana_backfill.py \\
        --config /etc/weewx/weewx.conf \\
        --until "2026-01-01 00:00:00" \\
        --output weewx_backfill.prom

See --help for all options.
"""

import argparse
import logging
import time

try:
    from user import grafana_metrics
except ImportError:
    import grafana_metrics

import configobj

import weewx.manager
import weewx.units

log = logging.getLogger(__name__)

_TIME_FORMATS = ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d')


def parse_timestamp(value):
    """Parse a CLI date/time argument into a Unix epoch timestamp (int).

    Accepts a bare integer (epoch seconds), or a local date/time in one of
    'YYYY-MM-DD HH:MM:SS', 'YYYY-MM-DD HH:MM', or 'YYYY-MM-DD'.
    """
    try:
        return int(value)
    except ValueError:
        pass
    for fmt in _TIME_FORMATS:
        try:
            return int(time.mktime(time.strptime(value, fmt)))
        except ValueError:
            continue
    raise ValueError("Unrecognized date/time %r; use YYYY-MM-DD[ HH:MM:SS] "
                      "or an epoch timestamp" % value)


def resolve_station_and_model(config_dict, station_arg=None, model_arg=None):
    """Resolve the 'station' and 'model' labels the same way the live
    uploader would, so a backfill lines up with the live series without
    having to specify anything on the command line.

    Precedence: explicit CLI argument, then [StdRESTful][[GrafanaCloud]]
    (if the live uploader is configured), then [Station], then a fallback.
    """
    grafana_cfg = config_dict.get('StdRESTful', {}).get('GrafanaCloud', {})
    station_cfg = config_dict.get('Station', {})
    station = station_arg or grafana_cfg.get('station') or station_cfg.get('location') or 'weewx'
    model = model_arg or grafana_cfg.get('model') or station_cfg.get('station_type') or 'weewx'
    return station, model


def collect_families(records, station, model, unit_system_override=None,
                      inputs=None, skip_fields=None):
    """Consume an iterable of weewx record dicts and return (families,
    count): 'families' is the mapping expected by
    grafana_metrics.write_openmetrics(), and 'count' the number of records
    that contributed at least one sample.

    Records with no 'usUnits' cannot be interpreted and are skipped, the
    same as the live uploader.
    """
    families = {}
    count = 0
    for record in records:
        if record.get('usUnits') is None:
            log.debug("skipping record at %s with no usUnits", record.get('dateTime'))
            continue
        if unit_system_override is not None:
            record = weewx.units.to_std_system(record, unit_system_override)
        unit_system = grafana_metrics.unit_system_name(record['usUnits'])
        labels = grafana_metrics.static_labels(station, model, unit_system)
        for name, _unit_type, value in grafana_metrics.iter_observations(
                record, inputs, skip_fields):
            families.setdefault(name, {'samples': []})['samples'].append(
                (labels, record['dateTime'], value))
        count += 1
    return families, count


def create_parser():
    parser = argparse.ArgumentParser(
        prog='grafana_backfill.py',
        description="Extract historical weewx archive data into OpenMetrics "
                     "format, for backfilling Grafana Cloud via promtool/mimirtool.",
        epilog="Next steps after running this tool:\n\n"
               "  promtool tsdb create-blocks-from openmetrics <output> ./blocks\n"
               "  mimirtool backfill --address=<mimir-url> --id=<tenant-id> ./blocks/*\n",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--config', required=True,
                         help="Path to weewx.conf")
    parser.add_argument('--binding', default='wx_binding',
                         help="Data binding to read from. Default: wx_binding")
    parser.add_argument('--since', default=None,
                         help="Only include records after this date/time (exclusive). "
                              "Accepts 'YYYY-MM-DD[ HH:MM:SS]' or an epoch timestamp. "
                              "Default: the earliest record in the database.")
    parser.add_argument('--until', required=True,
                         help="Only include records up to and including this date/time "
                              "(the backfill's cutoff point). Same format as --since.")
    parser.add_argument('--output', default='weewx_backfill.prom',
                         help="Output file, in OpenMetrics format. "
                              "Default: weewx_backfill.prom")
    parser.add_argument('--station', default=None,
                         help="Override the 'station' label. Default: read from "
                              "[StdRESTful][[GrafanaCloud]] or [Station] in weewx.conf.")
    parser.add_argument('--model', default=None,
                         help="Override the 'model' label. Default: read from "
                              "[StdRESTful][[GrafanaCloud]] or [Station] in weewx.conf.")
    parser.add_argument('--unit-system', default=None, choices=['US', 'METRIC', 'METRICWX'],
                         help="Convert every record to this weewx unit system before "
                              "export. Default: publish each record in whatever unit "
                              "system it is already stored in.")
    parser.add_argument('-v', '--verbose', action='store_true',
                         help="Log a line for every record that is skipped.")
    return parser


def main(argv=None):
    parser = create_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                         format='%(message)s')

    config_dict = configobj.ConfigObj(args.config, file_error=True, encoding='utf-8')
    station, model = resolve_station_and_model(config_dict, args.station, args.model)
    unit_system_override = (weewx.units.unit_constants[args.unit_system]
                             if args.unit_system else None)
    startstamp = parse_timestamp(args.since) if args.since else None
    stopstamp = parse_timestamp(args.until)

    log.info("station=%r model=%r binding=%r", station, model, args.binding)

    with weewx.manager.open_manager_with_config(config_dict, args.binding) as manager:
        records = manager.genBatchRecords(startstamp, stopstamp)
        families, count = collect_families(
            records, station, model, unit_system_override=unit_system_override)

    with open(args.output, 'w') as f:
        grafana_metrics.write_openmetrics(f, families)

    total_samples = sum(len(family['samples']) for family in families.values())
    log.info("wrote %d samples across %d metrics from %d archive records to %s",
              total_samples, len(families), count, args.output)
    log.info("next steps:\n"
              "  promtool tsdb create-blocks-from openmetrics %s ./blocks\n"
              "  mimirtool backfill --address=<mimir-url> --id=<tenant-id> ./blocks/*",
              args.output)


if __name__ == '__main__':
    main()