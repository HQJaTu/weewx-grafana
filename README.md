This is an extension to [weewx](https://weewx.com) that uploads weather data
to [Grafana Cloud](https://grafana.com/products/cloud/). The live uploader
(`bin/user/grafana.py`) does this using the
[OpenTelemetry Protocol](https://opentelemetry.io/docs/specs/otlp/) (OTLP),
over HTTP with JSON encoding, and does **not** use the Prometheus Remote
Write API.

It also includes a standalone tool, `bin/user/grafana_backfill.py`, for
backfilling Grafana Cloud with weather data that already exists in the
weewx database, from before the live uploader was installed. Unlike the
live uploader, the backfill tool pushes over Prometheus Remote Write --
see "Backfilling historical data" below.

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
records already stored in the weewx database and pushes them straight to
Grafana Cloud over the
[Prometheus Remote Write](https://prometheus.io/docs/concepts/remote_write_spec/)
protocol, using exactly the same metric names and labels as the live OTLP
uploader. Because a backfill and the live uploader publish the same series,
running a backfill up to a point in time and then starting the live uploader
produces one continuous history in Grafana Cloud, with no gap or overlap,
provided the `--until` cutoff lines up with when the live uploader started
publishing.

This is the only part of the extension that speaks Remote Write -- the live
uploader above still uses OTLP, and the "does **not** use the Prometheus
Remote Write API" statement at the top of this README only applies to that
live uploader.

Get a Remote Write URL alongside your existing OTLP endpoint: open your
stack's details page in Grafana Cloud, find the "Prometheus" card, and copy
its remote write endpoint (looks like
`https://prometheus-prod-*.grafana.net/api/prom/push`). Add it to the same
`[StdRESTful][[GrafanaCloud]]` section the live uploader already uses:

Token is a Cloud Access Policy token (scoped with the *metrics:write* permission).

```ini
[StdRESTful]
    [[GrafanaCloud]]
        otlp_endpoint = https://otlp-gateway-prod-xx-xxxx-0.grafana.net/otlp/v1/metrics
        prometheus_url = https://prometheus-prod-xx-xxxx-0.grafana.net/api/prom/push
        instance_id = 123456
        api_key = glc_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
        station = MyStation
        model = Vantage Pro2
```

`instance_id` and `api_key` are reused as-is for Remote Write's HTTP Basic
Auth, the same way the live uploader uses them for OTLP.

```
PYTHONPATH=/usr/share/weewx python3 bin/user/grafana_backfill.py \
    --weewx-config /etc/weewx/weewx.conf \
    --until "2026-01-01 00:00:00"
```

By default this streams matching records straight from the weewx database
in chronological order and pushes them in batches of `--batch-size` samples
(default 500) -- nothing is collected in memory first. `--until` (required)
is the cutoff point for the backfill: only records up to and including that
date/time are included. `--since` (optional) excludes everything at or
before a given date/time; omit it to start from the earliest record in the
database. Both accept `YYYY-MM-DD`, `YYYY-MM-DD HH:MM:SS`, or a raw epoch
timestamp. `station` and `model` are picked up automatically from
`[StdRESTful][[GrafanaCloud]]` (or `[Station]`) in weewx.conf, matching the
live uploader; override them with `--station`/`--model` if needed.
`prometheus_url`/`instance_id`/`api_key` can each be overridden per-run with
`--prometheus-url`/`--instance-id`/`--api-key`, e.g. to run without a
weewx.conf at all. See `--help` for the full option list, including
`--binding` and `--unit-system`.

**Direct push has a hard limit: Mimir's out-of-order ingestion window.**
Grafana Cloud's Mimir backend rejects any Remote Write sample older than a
per-tenant window (commonly on the order of hours, not years) with an HTTP
400 `err-mimir-sample-timestamp-too-old` error -- and this is not a
transient failure, so the tool detects it and stops immediately with an
explanatory message instead of retrying (retrying an out-of-order batch
unchanged can never succeed). In practice this means **direct push is only
useful for recent gaps** -- a few days to catch the live uploader up, say.
For a real backfill of months or years of history, expect every batch to be
rejected once it reaches data older than the window, and use the
OpenMetrics/promtool/mimirtool flow below instead: it uploads TSDB blocks
directly to storage, bypassing the ingester's ordering rules entirely, so
it has no such age limit. If you're not sure which applies to you, push a
small `--since`/`--until` range first and confirm it lands in Grafana Cloud;
if you want direct push to reach further back, ask Grafana support whether
your stack's out-of-order window can be widened, but for large/old
backfills the promtool/mimirtool flow is the recommended approach.

Pass `--dry-run` to skip pushing to Grafana Cloud entirely (combine with
`--output-file`, below, to only produce a file). A batch that fails with a
server error (5xx, 429) is retried a few times with a short backoff; a
batch Grafana Cloud rejects outright (any other 4xx) is not retried, since
retrying it unchanged would fail the same way, and the tool exits
immediately with the error Grafana Cloud returned.

#### Getting an OpenMetrics file instead

Pass `--output-file <path>` to also (or with `--dry-run`, instead) write the
data out as an [OpenMetrics](https://github.com/OpenMetrics/OpenMetrics)
text file -- useful for archival, or for feeding the older
[promtool](https://prometheus.io/docs/prometheus/latest/command-line/promtool/)/
[mimirtool](https://grafana.com/docs/mimir/latest/manage/tools/mimirtool/)
TSDB-block-upload flow instead of pushing directly:

```
PYTHONPATH=/usr/share/weewx python3 bin/user/grafana_backfill.py \
    --weewx-config /etc/weewx/weewx.conf \
    --until "2026-01-01 00:00:00" \
    --dry-run --output-file weewx_backfill.prom

promtool tsdb create-blocks-from openmetrics --max-block-duration=24h weewx_backfill.prom ./blocks
mimirtool backfill --address=<mimir-url> --id=<tenant-id> ./blocks/*
```

`<mimir-url>` and `<tenant-id>` (your Grafana Cloud instance ID) are shown
alongside the OTLP endpoint on the same stack details page; mimirtool
authenticates with `--id` (the tenant) and `instance_id:api_key` as HTTP
Basic Auth, the same `api_key` configured above. See mimirtool's `backfill`
documentation for the exact authentication flags for your version. Building
TSDB blocks for a large history is far slower than pushing directly over
Remote Write (easily well over an hour, versus minutes), which is why the
direct push above is the tool's default behavior.

#### Links
* `promtool`: https://prometheus.io/download/
  * Practically: `dnf install prometheus`
* `mimirtool`: https://github.com/grafana/mimir/
  * Practically: `dnf install https://github.com/grafana/mimir/releases/download/mimir-3.2.0/mimirtool-3.2.0.x86_64.rpm`

#### Dependencies

Because it speaks Remote Write, the backfill tool needs two packages beyond
what `weectl extension install` pulls in for the live uploader:

```
pip install cramjam protobuf
```

`cramjam` Snappy-compresses each batch, as Remote Write requires; `protobuf`
encodes the `WriteRequest` payload. This was developed and tested against
protobuf 3.19.6, matching the `protoc` version that generated
`bin/user/remote_write_pb2.py`; if a newer `protobuf` install reports a
descriptor/version mismatch, either install 3.19.6 or regenerate
`remote_write_pb2.py` against your installed version (see the regeneration
command at the top of `bin/user/remote_write.proto`).

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