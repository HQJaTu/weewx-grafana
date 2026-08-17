This is an extension to [weewx](https://weewx.com) that uploads weather data
to [Grafana Cloud](https://grafana.com/products/cloud/) using the
[OpenTelemetry Protocol](https://opentelemetry.io/docs/specs/otlp/) (OTLP),
over HTTP with JSON encoding. It does **not** use the Prometheus Remote
Write API.

It also includes a standalone converter utility for backfilling Grafana
Cloud with weather data that already exists in the weewx database, from
before the live uploader was installed.

### How data is named and labeled

Every weewx observation is published as its own OTLP metric, named
`weewx_<observation>_<unit>`, e.g. `outTemp` in degrees Celsius becomes:

```
weewx_outdoor_temperature_celsius
```

Baking the unit into the metric name (rather than into a label) follows the
usual Prometheus/OpenTelemetry convention and means the number stored is
never ambiguous, even if the station's configured units change later.

Three **static labels** (OTLP resource attributes) are attached to every
metric published by a given weewx installation:

| Label         | Meaning                                                       |
|---------------|----------------------------------------------------------------|
| `station`     | Station identifier, from the `station` config option           |
| `model`       | Weather station hardware model, from the `model` config option |
| `unit_system` | The unit system the values are published in (`US`, `METRIC`, or `METRICWX`) |

The full mapping from weewx observation type to metric base name, and from
weewx unit type to metric name suffix, lives in `bin/user/grafana_metrics.py`
(`OBS_METRIC_NAMES` and `UNIT_METRIC_SUFFIX`). Observation types not listed
in `OBS_METRIC_NAMES` (e.g. `extraTemp1`, `soilMoist3`) still get a
predictable name, via a camelCase-to-snake_case fallback, e.g. `extraTemp1`
becomes `extra_temp_1`.

### Download

```
wget -O weewx-grafana.zip https://github.com/HQJaTu/weewx-grafana/archive/master.zip
```

### How to Install

1. Run the extension installer:

    ```
    sudo weectl extension install weewx-grafana.zip
    ```

2. Get your OTLP endpoint URL, instance ID, and an API token from Grafana
   Cloud: open your stack's details page, find the "OpenTelemetry (OTLP)"
   card, and use "Generate now" to create an access policy token scoped for
   metrics write. The card shows the endpoint URL and your instance ID.

3. Modify the weewx configuration file:

    ```ini
    [StdRESTful]
        [[GrafanaCloud]]
            otlp_endpoint = https://otlp-gateway-prod-xx-xxxx-0.grafana.net/otlp/v1/metrics
            instance_id = 123456
            api_key = glc_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
            station = MyStation
            model = Vantage Pro2
    ```

4. Restart weewx:

    ```
    sudo systemctl restart weewx
    ```

### Options

_otlp_endpoint_ - Full URL of the Grafana Cloud OTLP metrics endpoint,
including the `/v1/metrics` path. Required.

_instance_id_ - Your Grafana Cloud stack/instance ID. Used as the username
of the HTTP Basic Authorization header that OTLP gateway requires. Required.

_api_key_ - A Grafana Cloud access policy token with metrics write scope.
Used as the password of the Basic Authorization header. Required.

_station_ - Static `station` label published on every metric. Default: the
station `location` from `[Station]` in weewx.conf, or `weewx` if that is
also unset.

_model_ - Static `model` label published on every metric. Default: the
station hardware/driver name, or `weewx` if that is unset.

_unit_system_ - Convert every observation to this weewx unit system before
publishing. Options are `US`, `METRIC`, or `METRICWX`. Default is unset:
publish whatever unit system the record/database is already in.

_binding_ - Whether to bind to LOOP packets (`loop`), archive records
(`archive`), or both. Default is `archive`.

_data_binding_ - Which weewx data binding to use when augmenting a record
with `hourRain`/`rain24`/`dayRain` if they are missing from it. Default is
`wx_binding`.

_skip_fields_ - Comma-separated list of observation types to never publish,
in addition to the always-excluded `usUnits`, `interval`, and `dateTime`.

Use the `inputs` map to customize the unit or the full published name of any
observation, exactly as in [weewx-mqtt](https://github.com/matthewwall/weewx-mqtt):

```ini
[StdRESTful]
    [[GrafanaCloud]]
        ...
        [[[inputs]]]
            [[[[outTemp]]]]
                unit = degree_F   # always publish outTemp in Fahrenheit,
                                  # regardless of unit_system
            [[[[barometer]]]]
                name = weewx_station_pressure_hpa  # take full control of
                                                     # the published name
```

### Connection robustness

Publishing uses weewx's standard RESTful posting framework
(`weewx.restx.RESTThread`): each record is retried up to `max_tries` times
(default 3), waiting `retry_wait` seconds (default 5) between attempts, with
a `timeout` (default 10 seconds) per attempt. A record that cannot be
delivered after `max_tries` attempts is logged as a failure and dropped;
it is not retried on the next archive interval.

Grafana Cloud's OTLP gateway can report a partial ingestion failure with an
HTTP 200 response and a `partialSuccess` object describing rejected data
points; this extension treats a non-empty `partialSuccess` the same as an
HTTP error, so it is logged and retried like any other failed post.

### Backfilling historical data

`bin/user/grafana_backfill.py` is a standalone command-line tool that reads
records already stored in the weewx database and writes them out in
[OpenMetrics](https://github.com/OpenMetrics/OpenMetrics) text exposition
format, using exactly the same metric names and labels as the live
uploader. It does not talk to Grafana Cloud itself.

```
PYTHONPATH=/usr/share/weewx python3 bin/user/grafana_backfill.py \
    --config /etc/weewx/weewx.conf \
    --until "2026-01-01 00:00:00" \
    --output weewx_backfill.prom
```

`--until` (required) is the cutoff point for the export: only records up to
and including that date/time are included. `--since` (optional) excludes
everything at or before a given date/time; omit it to start from the
earliest record in the database. Both accept `YYYY-MM-DD`,
`YYYY-MM-DD HH:MM:SS`, or a raw epoch timestamp. `station` and `model` are
picked up automatically from `[StdRESTful][[GrafanaCloud]]` (or
`[Station]`) in weewx.conf, matching the live uploader; override them with
`--station`/`--model` if needed. See `--help` for the full option list,
including `--binding` and `--unit-system`.

Once you have the OpenMetrics file, convert it to TSDB blocks and upload it
with the standard Prometheus/Mimir tools
([promtool](https://prometheus.io/docs/prometheus/latest/command-line/promtool/),
[mimirtool](https://grafana.com/docs/mimir/latest/manage/tools/mimirtool/)):

```
promtool tsdb create-blocks-from openmetrics weewx_backfill.prom ./blocks
mimirtool backfill --address=<mimir-url> --id=<tenant-id> ./blocks/*
```

`<mimir-url>` and `<tenant-id>` (your Grafana Cloud instance ID) are shown
alongside the OTLP endpoint on the same "OpenTelemetry" card in your Grafana
Cloud stack's details page; mimirtool authenticates with `--id` (the tenant)
and `instance_id:api_key` as HTTP Basic Auth, the same `api_key` used by the
live uploader (create it with `metrics:write` scope). See mimirtool's
`backfill` documentation for the exact authentication flags for your
version.

Because a backfill and the live uploader publish the same series (same
metric names, same labels), running a backfill up to a point in time and
then starting the live uploader produces one continuous history in Grafana
Cloud with no gap or overlap, provided the `--until` cutoff lines up with
when the live uploader started publishing.

For a very large history, run the export in chunks with `--since`/`--until`
and a separate `--output` file per chunk, since `grafana_backfill.py`
collects all matching samples in memory before writing (OpenMetrics
requires every sample for a metric to be written contiguously, so it cannot
stream record-by-record the way the live uploader does).

### Running the tests

The tests are broker/network-free: they patch `urllib.request.urlopen` and
operate on in-memory records, so no Grafana Cloud account or running weewx
instance is required.

```
PYTHONPATH=/path/to/weewx/src python bin/user/tests/test_grafana_metrics.py
PYTHONPATH=/path/to/weewx/src python bin/user/tests/test_grafana_service.py
PYTHONPATH=/path/to/weewx/src python bin/user/tests/test_grafana_backfill.py
```

(`/path/to/weewx/src` is weewx's own source tree, needed for `import
weewx`, `weewx.restx`, `weewx.units`, and `weewx.manager`.)