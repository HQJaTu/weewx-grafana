# Copyright 2026 - Distributed under the terms of the GNU Public License (GPLv3)
"""Upload weather data to Grafana Cloud using the OpenTelemetry Protocol
(OTLP), over HTTP/JSON.

This is the live-data counterpart to bin/user/grafana_backfill.py, which
extracts historical data from the weewx database in the same metric naming
scheme for one-time backfill via promtool/mimirtool.

Minimal configuration:

[StdRESTful]
    [[GrafanaCloud]]
        otlp_endpoint = https://otlp-gateway-prod-xx-xxxx-0.grafana.net/otlp/v1/metrics
        instance_id = 123456
        api_key = INSERT_API_KEY_HERE

'otlp_endpoint', 'instance_id', and 'api_key' come from a Grafana Cloud
stack's "OpenTelemetry" connection page. 'instance_id' and 'api_key' are used
as the username and password of the HTTP Basic Authorization header, which is
how the OTLP gateway authenticates a Grafana Cloud stack.

Other options can be specified:

[StdRESTful]
    [[GrafanaCloud]]
        ...
        # Static labels published as OTLP resource attributes on every
        # metric. 'station' identifies this weewx installation, 'model' the
        # hardware. Defaults come from the [Station] section of weewx.conf.
        station = MyStation
        model = Vantage Pro2

        # Convert every observation to this weewx unit system before
        # publishing. Options are US, METRIC, or METRICWX. Default is None:
        # publish whatever unit system the database/StdConvert is using.
        unit_system = METRIC

        # Bind to LOOP packets ('loop'), archive records ('archive'), or
        # both. Default is 'archive'.
        binding = archive

        # Observation types to never publish, in addition to the always
        # excluded 'usUnits', 'interval', and 'dateTime'.
        skip_fields = rxCheckPercent, signal1

        # To change the data binding used to augment records with
        # hourRain/rain24/dayRain when they are missing from the record:
        data_binding = wx_binding

Use the inputs map to customize the name or unit of any observation, exactly
as in weewx-mqtt:

[StdRESTful]
    [[GrafanaCloud]]
        ...
        [[[inputs]]]
            [[[[outTemp]]]]
                unit = degree_F   # publish outTemp in degree_F regardless
                                  # of unit_system
            [[[[barometer]]]]
                name = weewx_pressure_custom_name  # full control of the
                                                    # published metric name
"""

import base64
import json
import logging
import sys

try:
    from user import grafana_metrics
except ImportError:
    import grafana_metrics

import weewx
import weewx.manager
import weewx.restx
import weewx.units
from weeutil.weeutil import to_int

try:
    import queue as Queue
except ImportError:
    import Queue

VERSION = grafana_metrics.VERSION

log = logging.getLogger(__name__)

if weewx.__version__ < "4":
    raise weewx.UnsupportedFeature("weewx 4 is required, found %s" % weewx.__version__)


def _parse_inputs(raw_inputs):
    """Normalize the ConfigObj [[[inputs]]] section into a plain dict."""
    inputs = {}
    for obs_key in raw_inputs:
        entry = dict(raw_inputs[obs_key])
        # weewx-mqtt accepted 'units' as an alias for 'unit'; do the same
        # here for anyone copying a config from that extension.
        if 'units' in entry and 'unit' not in entry:
            entry['unit'] = entry.pop('units')
        inputs[obs_key] = entry
    return inputs


def _parse_skip_fields(raw):
    if raw is None:
        return set()
    if isinstance(raw, str):
        return {raw}
    return set(raw)


class GrafanaCloud(weewx.restx.StdRESTful):
    """weewx service that publishes archive/loop data to Grafana Cloud over
    OTLP/HTTP."""

    def __init__(self, engine, config_dict):
        super(GrafanaCloud, self).__init__(engine, config_dict)
        log.info("service version is %s", VERSION)

        site_dict = weewx.restx.get_site_dict(
            config_dict, 'GrafanaCloud', 'otlp_endpoint', 'instance_id', 'api_key')
        if not site_dict:
            return

        stn = getattr(self.engine, 'stn_info', None)
        site_dict.setdefault('station', getattr(stn, 'location', None) or 'weewx')
        site_dict.setdefault('model', getattr(stn, 'hardware', None) or 'weewx')
        site_dict['station'] = grafana_metrics.coerce_config_string(site_dict['station'])
        site_dict['model'] = grafana_metrics.coerce_config_string(site_dict['model'])

        if 'inputs' in config_dict['StdRESTful']['GrafanaCloud']:
            site_dict['inputs'] = _parse_inputs(
                config_dict['StdRESTful']['GrafanaCloud']['inputs'])

        site_dict['skip_fields'] = _parse_skip_fields(site_dict.get('skip_fields'))

        usn = site_dict.get('unit_system', None)
        if usn is not None:
            site_dict['unit_system'] = weewx.units.unit_constants[usn.upper()]

        binding = site_dict.pop('binding', 'archive')
        data_binding = site_dict.pop('data_binding', 'wx_binding')

        try:
            site_dict['manager_dict'] = weewx.manager.get_manager_dict_from_config(
                config_dict, data_binding)
        except weewx.UnknownBinding:
            pass

        self.archive_queue = Queue.Queue()
        self.archive_thread = GrafanaThread(self.archive_queue, **site_dict)
        self.archive_thread.start()

        if 'archive' in binding:
            self.bind(weewx.NEW_ARCHIVE_RECORD, self.new_archive_record)
        if 'loop' in binding:
            self.bind(weewx.NEW_LOOP_PACKET, self.new_loop_packet)

        log.info("station is %s, model is %s", site_dict['station'], site_dict['model'])
        log.info("data will be uploaded to %s", site_dict['otlp_endpoint'])

    def new_archive_record(self, event):
        self.archive_queue.put(event.record)

    def new_loop_packet(self, event):
        self.archive_queue.put(event.packet)


class GrafanaThread(weewx.restx.RESTThread):

    def __init__(self, queue, otlp_endpoint, instance_id, api_key,
                 station='weewx', model='weewx', unit_system=None,
                 inputs=None, skip_fields=None,
                 manager_dict=None, post_interval=None, stale=None,
                 log_success=True, log_failure=True,
                 timeout=10, max_tries=3, retry_wait=5,
                 skip_upload=False, max_backlog=sys.maxsize):
        """
        otlp_endpoint (str): Full URL of the Grafana Cloud OTLP metrics
            endpoint, e.g. https://otlp-gateway-.../otlp/v1/metrics
        instance_id (str): Grafana Cloud stack/instance id. Used as the
            HTTP Basic Auth username.
        api_key (str): Grafana Cloud access policy token. Used as the HTTP
            Basic Auth password.
        station (str): Static 'station' label/resource attribute.
        model (str): Static 'model' label/resource attribute.
        unit_system (int|None): A weewx standard unit system constant
            (weewx.US, weewx.METRIC, weewx.METRICWX) to convert every record
            to before publishing. None publishes whatever unit system the
            record is already in.
        inputs (dict|None): obs_key -> {'name': ..., 'unit': ...} overrides.
        skip_fields (set|None): obs_keys to never publish.
        """
        super(GrafanaThread, self).__init__(
            queue,
            protocol_name='GrafanaCloud',
            manager_dict=manager_dict,
            post_interval=post_interval,
            max_backlog=max_backlog,
            stale=stale,
            log_success=log_success,
            log_failure=log_failure,
            timeout=timeout,
            max_tries=max_tries,
            retry_wait=retry_wait,
            skip_upload=skip_upload)
        self.otlp_endpoint = otlp_endpoint
        self.station = station
        self.model = model
        self.unit_system = to_int(unit_system) if unit_system is not None else None
        self.inputs = inputs or {}
        self.skip_fields = skip_fields or set()
        credentials = ("%s:%s" % (instance_id, api_key)).encode('utf-8')
        self._auth_header = "Basic " + base64.b64encode(credentials).decode('ascii')

    def get_record(self, record, dbmanager):
        # Every downstream step (augmenting, converting, naming) is driven by
        # usUnits, which every loop packet and archive record carries. Check
        # it before calling the superclass, which would otherwise raise an
        # unhandled KeyError deep inside a database query.
        if record.get('usUnits') is None:
            raise weewx.restx.AbortedPost(
                "record has no 'usUnits'; cannot determine units")
        record = super(GrafanaThread, self).get_record(record, dbmanager)
        if self.unit_system is not None:
            record = weewx.units.to_std_system(record, self.unit_system)
        return record

    def format_url(self, _record):
        return self.otlp_endpoint

    def get_request(self, url):
        request = super(GrafanaThread, self).get_request(url)
        request.add_header('Authorization', self._auth_header)
        return request

    def get_post_body(self, record):
        unit_system = grafana_metrics.unit_system_name(record['usUnits'])
        payload = grafana_metrics.build_otlp_payload(
            record, self.station, self.model, unit_system,
            inputs=self.inputs, skip_fields=self.skip_fields)
        metrics = payload['resourceMetrics'][0]['scopeMetrics'][0]['metrics']
        if not metrics:
            raise weewx.restx.AbortedPost("no numeric observations to publish")
        return json.dumps(payload), 'application/json'

    def check_response(self, response):
        """OTLP/HTTP reports partial ingestion failures with HTTP 200 and a
        'partialSuccess' object in the JSON body; treat a non-empty one as a
        failed post so it gets logged (and retried) like any other failure.
        """
        raw = response.read()
        if not raw:
            return
        try:
            body = json.loads(raw.decode('utf-8'))
        except ValueError:
            return
        partial = body.get('partialSuccess')
        if partial and partial.get('rejectedDataPoints'):
            raise weewx.restx.FailedPost(
                "Grafana Cloud rejected %s data point(s): %s" %
                (partial['rejectedDataPoints'], partial.get('errorMessage')))