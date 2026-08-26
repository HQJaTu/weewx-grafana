# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A [weewx](https://weewx.com) extension that uploads weather station data to Grafana Cloud. It has two independent upload paths that deliberately use two different wire protocols:

- `bin/user/grafana.py` — the live uploader, a `weewx.restx.StdRESTful` service that runs inside the weewx daemon and pushes each new archive/loop record over **OTLP/HTTP (JSON)**.
- `bin/user/grafana_backfill.py` — a standalone CLI that reads historical records straight out of the weewx database and pushes them over **Prometheus Remote Write** (protobuf `WriteRequest`, Snappy-compressed). It exists because OTLP is a live-streaming protocol with no backfill concept, and because Remote Write's ingester enforces an out-of-order/too-old window that OTLP doesn't need to worry about for freshly-generated data.

Both paths must produce byte-identical metric names and label sets so a backfill and the live uploader stitch into one continuous series in Grafana Cloud. That shared naming/labeling/encoding logic lives in `bin/user/grafana_metrics.py` — treat it as the single source of truth; never duplicate naming logic into either uploader.

## Commands

Install the runtime dependency the backfill tool needs beyond what `weectl extension install` pulls in for the live uploader:

```
pip install -r requirements.txt
```

Run the full test suite (network-free — tests patch `urllib.request.urlopen` and use in-memory records, no live weewx or Grafana Cloud account needed):

```
PYTHONPATH=/path/to/weewx/src python bin/user/tests/test_grafana_metrics.py
PYTHONPATH=/path/to/weewx/src python bin/user/tests/test_grafana_service.py
PYTHONPATH=/path/to/weewx/src python bin/user/tests/test_grafana_backfill.py
```

`/path/to/weewx/src` must be weewx's own source tree (needed for `import weewx`, `weewx.restx`, `weewx.units`, `weewx.manager`). In CI this is satisfied by `pip install weewx` instead (see `.github/workflows/tests.yml`).

Run a single test class or method with `unittest`'s discovery syntax, e.g.:

```
PYTHONPATH=/path/to/weewx/src python -m unittest bin.user.tests.test_grafana_backfill.PushWriteRequestTest
PYTHONPATH=/path/to/weewx/src python -m unittest bin.user.tests.test_grafana_backfill.PushWriteRequestTest.test_connection_error_is_retried_then_raises
```

Building the optional Go companion tool (`tools/tsdb-block-writer`, not part of the Python extension or its installer — see that directory's own README for why):

```
cd tools/tsdb-block-writer && go build -o tsdb-block-writer .
```

There is no lint config in this repo.

## Architecture

**`bin/user/grafana_metrics.py`** is imported by both uploaders and owns everything about how a weewx record becomes a metric:
- `OBS_METRIC_NAMES` / `UNIT_METRIC_SUFFIX` map weewx observation types and unit types to the `weewx_<observation>_<unit>` naming convention (e.g. `outTemp` in Celsius → `weewx_outdoor_temperature_celsius`); observation types not in `OBS_METRIC_NAMES` fall back to a camelCase→snake_case conversion.
- `static_labels()` builds the three labels/resource attributes (`station`, `model`, `unit_system`) attached to every metric.
- `iter_observations()` is the shared per-record iterator (unit conversion via `[[[inputs]]]` overrides, `skip_fields` filtering) that both `build_otlp_payload()` (live) and `grafana_backfill.py`'s family-collection helpers consume — this is the point of convergence that keeps the two paths' output identical.
- `build_otlp_payload()` encodes for the live uploader; `write_openmetrics()` and `build_write_request()`/`compress_snappy()` encode for the backfill tool's two output modes (OpenMetrics text file vs. Remote Write push).
- `remote_write_pb2.py` is a generated protobuf module for the Remote Write `WriteRequest` schema; `remote_write.proto` carries the regeneration command in its header comment if it ever needs to be rebuilt against a different `protobuf` version (the project was developed/tested against protobuf 3.19.6, so a newer install can raise a descriptor/version mismatch).

**`bin/user/grafana.py`** wires into weewx's own service framework rather than doing its own I/O loop: `GrafanaCloud(weewx.restx.StdRESTful)` reads `[StdRESTful][[GrafanaCloud]]` config and starts a `GrafanaThread(weewx.restx.RESTThread)` on a queue, binding to `NEW_ARCHIVE_RECORD`/`NEW_LOOP_PACKET` per the `binding` option. Retry/backoff/timeout behavior (`max_tries`, `retry_wait`, `timeout`) comes from the base `RESTThread` for free. `check_response()` additionally parses OTLP's `partialSuccess` field out of an HTTP 200 body and raises `FailedPost` if any data points were rejected, so a partial ingestion failure is retried like any other failure instead of being silently swallowed.

**`bin/user/grafana_backfill.py`** is a plain argparse CLI, not a weewx service. Its `main()` streams `manager.genBatchRecords()` in chronological order via `iter_family_batches()` and flushes a `WriteRequest` batch every `--batch-size` samples — records must stay time-ordered because Remote Write requires non-decreasing timestamps per series, both within one `WriteRequest` and across successive ones, which is why this streams rather than collecting everything in memory first. `push_write_request()` detects Mimir's out-of-order/too-old rejection (`err-mimir-sample-timestamp-too-old` and similar substrings) and raises immediately with a pointer to the block-upload flow instead of retrying — that rejection can never succeed by retrying unchanged, unlike a 5xx/429 which is retried with backoff. `--output-file` is an independent side path (via `collect_families()`/`write_openmetrics()`) for producing an OpenMetrics text file, used either for archival or as input to the TSDB-block-upload flow for history too old for direct push.

**`tools/tsdb-block-writer`** (Go, separate from the Python extension, not referenced by `install.py`) is an optional faster replacement for `promtool tsdb create-blocks-from` when turning an OpenMetrics file into TSDB blocks for `mimirtool backfill` — see its own README for why the TSDB block format can't be produced from Python at all (no maintained Python implementation exists; it's Go-only, part of `prometheus/prometheus`).

**`install.py`** is a `weecfg.extension.ExtensionInstaller`; its `files=[...]` manifest controls exactly what `weectl extension install` copies into `bin/user/` on a user's weewx install — currently `grafana.py`, `grafana_metrics.py`, `grafana_backfill.py`. `remote_write_pb2.py` is imported by `grafana_metrics.py`/`grafana_backfill.py` but is not itself listed there since the backfill tool is meant to be run from a checkout, not from an installed weewx tree. `VERSION` here is the single source of truth for the extension's version — bumping it is required for every PR into `master` (enforced by `.github/workflows/version-check.yml`) and drives both the post-merge git tag (`tag-release-candidate.yml`) and the release zip (`release.yml`).

## CI/CD

Three GitHub Actions workflows drive the release lifecycle end to end, all keyed off `install.py`'s `VERSION`:
1. `version-check.yml` — on every PR into `master`, fails unless `VERSION` was bumped upward from `master`'s copy (skippable with a `norelease` label). This is a required status check via branch protection on `master`.
2. `tag-release-candidate.yml` — on merge to `master`, tags the merge commit `v<VERSION>` (skips the same way on `norelease`, and guards against re-tagging if that tag already exists).
3. `release.yml` — on a GitHub Release being created (or manual `workflow_dispatch`), packages `install.py`, `changelog`, `LICENSE`, `README.md`, and the three `bin/user/*.py` files into a versioned zip and attaches it to the release.

`tests.yml` runs the three test files above on every push; it installs both `weewx` and `requirements.txt` before running them.
