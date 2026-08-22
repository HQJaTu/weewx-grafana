#!/usr/bin/env python3
# Copyright 2026 - Distributed under the terms of the GNU Public License (GPLv3)
"""Backfill historical weewx archive data into Grafana Cloud, for data that
predates the live bin/user/grafana.py uploader.

By default this tool talks to Grafana Cloud directly, pushing batches over
the Prometheus Remote Write protocol (protobuf WriteRequest, Snappy
compressed), using exactly the same metric naming and labels as the live
uploader (see bin/user/grafana_metrics.py) so the backfilled series and the
live series are the same series in Grafana Cloud. This tool's live counterpart
(bin/user/grafana.py) still uses OTLP, not Remote Write -- only this
standalone backfill tool speaks Remote Write.

Pass --output-file to additionally (or, with --dry-run, instead) write the
data out as an OpenMetrics text file, e.g. for archival or as input to a
TSDB-block-upload flow (tools/tsdb-block-writer, or the older promtool/
mimirtool flow) for history too old for direct push.

Usage:

    PYTHONPATH=/path/to/weewx/src python3 bin/user/grafana_backfill.py \\
        --weewx-config /etc/weewx/weewx.conf \\
        --until "2026-01-01 00:00:00"

See --help for all options.
"""

import argparse
import base64
import logging
import time
import urllib.error
import urllib.request

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
    return (grafana_metrics.coerce_config_string(station),
            grafana_metrics.coerce_config_string(model))


def _add_record_samples(families, record, station, model, unit_system_override=None,
                         inputs=None, skip_fields=None):
    """Append one record's samples into 'families' (the mapping expected by
    grafana_metrics.write_openmetrics()/build_write_request()) in place.

    Returns the number of samples added, or None if the record has no
    'usUnits' and so cannot be interpreted at all, the same as the live
    uploader (as opposed to 0, meaning the record was interpretable but
    happened to contribute no numeric observations).
    """
    if record.get('usUnits') is None:
        log.debug("skipping record at %s with no usUnits", record.get('dateTime'))
        return None
    if unit_system_override is not None:
        record = weewx.units.to_std_system(record, unit_system_override)
    unit_system = grafana_metrics.unit_system_name(record['usUnits'])
    labels = grafana_metrics.static_labels(station, model, unit_system)
    added = 0
    for name, _unit_type, value in grafana_metrics.iter_observations(
            record, inputs, skip_fields):
        families.setdefault(name, {'samples': []})['samples'].append(
            (labels, record['dateTime'], value))
        added += 1
    return added


def collect_families(records, station, model, unit_system_override=None,
                      inputs=None, skip_fields=None):
    """Consume an iterable of weewx record dicts and return (families,
    count): 'families' is the mapping expected by
    grafana_metrics.write_openmetrics(), and 'count' the number of records
    that contributed at least one sample.
    """
    families = {}
    count = 0
    for record in records:
        added = _add_record_samples(families, record, station, model,
                                     unit_system_override, inputs, skip_fields)
        if added is not None:
            count += 1
    return families, count


def iter_family_batches(records, station, model, batch_size,
                         unit_system_override=None, inputs=None, skip_fields=None):
    """Like collect_families(), but yields (families, count) chunks instead
    of collecting the whole range in memory: a chunk is flushed as soon as it
    holds at least 'batch_size' samples (plus a final, possibly smaller chunk
    at the end).

    'records' must be consumed in chronological order (as
    weewx.manager.Manager.genBatchRecords() yields them) so that both the
    chunks themselves, and every series within them, stay in non-decreasing
    timestamp order -- required by Remote Write.
    """
    families = {}
    count = 0
    sample_total = 0
    for record in records:
        added = _add_record_samples(families, record, station, model,
                                     unit_system_override, inputs, skip_fields)
        if added is not None:
            count += 1
            sample_total += added
        if sample_total >= batch_size:
            yield families, count
            families, count, sample_total = {}, 0, 0
    if families:
        yield families, count


def resolve_push_config(config_dict, prometheus_url_arg=None,
                         instance_id_arg=None, api_key_arg=None):
    """Resolve the Prometheus Remote Write push target the same way
    station/model are resolved: explicit CLI argument, then
    [StdRESTful][[GrafanaCloud]] in weewx.conf.

    Returns (prometheus_url, instance_id, api_key), any of which may be
    None if not configured anywhere.
    """
    grafana_cfg = config_dict.get('StdRESTful', {}).get('GrafanaCloud', {})
    prometheus_url = prometheus_url_arg or grafana_cfg.get('prometheus_url')
    instance_id = instance_id_arg or grafana_cfg.get('instance_id')
    api_key = api_key_arg or grafana_cfg.get('api_key')
    return prometheus_url, instance_id, api_key


def build_auth_header(instance_id, api_key):
    """Build the HTTP Basic Authorization header value Grafana Cloud expects,
    the same way the live uploader (bin/user/grafana.py) does."""
    credentials = ("%s:%s" % (instance_id, api_key)).encode('utf-8')
    return "Basic " + base64.b64encode(credentials).decode('ascii')



# Substrings Mimir's ingester uses when it rejects a sample as older than the
# tenant's configured out-of-order time window. Direct Remote Write push can
# never succeed for a batch rejected this way -- retrying won't help, and
# nor will anything client-side except backfilling that data a different way.
_OUT_OF_ORDER_MARKERS = (
    'out-of-order', 'out of order', 'err-mimir-sample-timestamp-too-old',
)


def push_write_request(write_request, prometheus_url, auth_header,
                        timeout=10, max_tries=3, retry_wait=5):
    """POST one WriteRequest to a Prometheus Remote Write endpoint, Snappy
    compressed as the protocol requires. Retries on 5xx/429 responses and on
    connection errors; raises immediately on other 4xx responses, since
    retrying a rejected batch unchanged won't help.
    """
    body = grafana_metrics.compress_snappy(write_request.SerializeToString())
    for attempt in range(1, max_tries + 1):
        request = urllib.request.Request(prometheus_url, data=body, method='POST')
        request.add_header('Content-Type', 'application/x-protobuf')
        request.add_header('Content-Encoding', 'snappy')
        request.add_header('X-Prometheus-Remote-Write-Version', '0.1.0')
        request.add_header('Authorization', auth_header)
        try:
            response = urllib.request.urlopen(request, timeout=timeout)
            response.read()
            return
        except urllib.error.HTTPError as e:
            detail = e.read().decode('utf-8', 'replace')
            if e.code < 500 and e.code != 429:
                if any(marker in detail.lower() for marker in _OUT_OF_ORDER_MARKERS):
                    raise RuntimeError(
                        "Grafana Cloud rejected this batch (HTTP %d): the samples "
                        "are older than your stack's out-of-order ingestion window, "
                        "so a direct push can never deliver them, no matter how many "
                        "times it's retried. For historical data this old, use "
                        "--dry-run --output-file to produce an OpenMetrics file "
                        "instead, then turn it into TSDB blocks with "
                        "tools/tsdb-block-writer (recommended) or promtool, and "
                        "upload with mimirtool backfill (see this tool's README, "
                        "'Getting an OpenMetrics file instead'). "
                        "Original error: %s" % (e.code, detail))
                raise RuntimeError(
                    "Grafana Cloud rejected the batch (HTTP %d): %s" % (e.code, detail))
            log.warning("push attempt %d/%d failed (HTTP %d): %s",
                        attempt, max_tries, e.code, detail)
        except urllib.error.URLError as e:
            log.warning("push attempt %d/%d failed: %s", attempt, max_tries, e)
        if attempt < max_tries:
            time.sleep(retry_wait)
    raise RuntimeError(
        "failed to push batch to %s after %d attempts" % (prometheus_url, max_tries))


def create_parser():
    parser = argparse.ArgumentParser(
        prog='grafana_backfill.py',
        description="Backfill historical weewx archive data into Grafana Cloud, "
                     "pushing directly over the Prometheus Remote Write protocol.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--weewx-config', required=True,
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
    parser.add_argument('--output-file', default=None,
                         help="Also write the data out as an OpenMetrics text file at "
                              "this path (e.g. for archival, or as input to a "
                              "TSDB-block-upload flow: tools/tsdb-block-writer or "
                              "promtool/mimirtool). Independent of pushing: combine "
                              "with --dry-run to only write the file.")
    parser.add_argument('--dry-run', action='store_true',
                         help="Build and validate every batch (the same encoding work "
                              "a real push does) but don't actually push to Grafana "
                              "Cloud. Add --output-file to also get the data as a file.")
    parser.add_argument('--prometheus-url', default=None,
                         help="Prometheus Remote Write endpoint to push to. Default: "
                              "'prometheus_url' from [StdRESTful][[GrafanaCloud]] in "
                              "weewx.conf.")
    parser.add_argument('--instance-id', default=None,
                         help="Grafana Cloud stack/instance id (HTTP Basic Auth "
                              "username). Default: 'instance_id' from "
                              "[StdRESTful][[GrafanaCloud]] in weewx.conf.")
    parser.add_argument('--api-key', default=None,
                         help="Grafana Cloud API key (HTTP Basic Auth password). "
                              "Default: 'api_key' from [StdRESTful][[GrafanaCloud]] "
                              "in weewx.conf.")
    parser.add_argument('--batch-size', type=int, default=500,
                         help="Approximate number of samples to accumulate before "
                              "pushing a batch (a conservative default; raise it for "
                              "faster backfills once you've confirmed Grafana Cloud "
                              "accepts your batch size). Default: 500")
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

    config_dict = configobj.ConfigObj(args.weewx_config, file_error=True, encoding='utf-8')
    station, model = resolve_station_and_model(config_dict, args.station, args.model)
    unit_system_override = (weewx.units.unit_constants[args.unit_system]
                             if args.unit_system else None)
    startstamp = parse_timestamp(args.since) if args.since else None
    stopstamp = parse_timestamp(args.until)

    should_push = not args.dry_run
    prometheus_url, instance_id, api_key = resolve_push_config(
        config_dict, args.prometheus_url, args.instance_id, args.api_key)
    if should_push and not all((prometheus_url, instance_id, api_key)):
        parser.error(
            "pushing to Grafana Cloud requires 'prometheus_url', 'instance_id', and "
            "'api_key' (from [StdRESTful][[GrafanaCloud]] in weewx.conf, or "
            "--prometheus-url/--instance-id/--api-key). Pass --dry-run to skip "
            "pushing (combine with --output-file to just get the OpenMetrics file).")
    log.info("station=%r model=%r binding=%r", station, model, args.binding)

    with weewx.manager.open_manager_with_config(config_dict, args.binding) as manager:
        auth_header = build_auth_header(instance_id, api_key) if should_push else None
        total_samples = 0
        total_records = 0
        for families, count in iter_family_batches(
                manager.genBatchRecords(startstamp, stopstamp), station, model,
                args.batch_size, unit_system_override=unit_system_override):
            # Always build the WriteRequest, even on --dry-run: this is what
            # catches encoding problems (e.g. an unhashable label value)
            # before they'd otherwise only surface on a real push.
            write_request = grafana_metrics.build_write_request(families)
            batch_samples = sum(len(f['samples']) for f in families.values())
            total_samples += batch_samples
            total_records += count
            if should_push:
                push_write_request(write_request, prometheus_url, auth_header)
                log.info("pushed %d samples (%d records) to %s",
                         batch_samples, count, prometheus_url)
        if should_push:
            log.info("done: pushed %d samples across %d archive records",
                      total_samples, total_records)
        else:
            log.info("dry run: validated %d samples across %d archive records "
                      "(nothing pushed)", total_samples, total_records)

        if args.output_file:
            records = manager.genBatchRecords(startstamp, stopstamp)
            families, count = collect_families(
                records, station, model, unit_system_override=unit_system_override)
            with open(args.output_file, 'w') as f:
                grafana_metrics.write_openmetrics(f, families)
            total_samples = sum(len(family['samples']) for family in families.values())
            log.info("wrote %d samples across %d metrics from %d archive records to %s",
                      total_samples, len(families), count, args.output_file)


if __name__ == '__main__':
    main()