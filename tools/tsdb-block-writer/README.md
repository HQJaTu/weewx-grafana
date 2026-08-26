# tsdb-block-writer

**Status: prototype / experimental.** Not wired into `grafana_backfill.py`, not
covered by any test suite, and not part of the weewx-grafana extension's
installer. It's a standalone command-line tool you build and run yourself.

## What this is

A small Go program that reads the OpenMetrics file produced by
`bin/user/grafana_backfill.py --output-file` and writes native Prometheus
TSDB blocks directly, using the same `github.com/prometheus/prometheus/tsdb`
package Prometheus itself uses -- the same library `promtool` is built on.

It's the recommended (but optional) faster alternative to this step of the
documented backfill flow (see the project's main
[README.md](../../README.md), "Getting an OpenMetrics file instead"):

```
promtool tsdb create-blocks-from openmetrics --max-block-duration=24h weewx_backfill.prom ./blocks
```

`promtool`'s version of this command was measured taking over 100 minutes on
a real multi-year backfill. This tool does the same job -- OpenMetrics text
in, a directory of TSDB blocks out, ready for `mimirtool backfill` -- driven
directly through the block-writing library instead of through promtool's CLI
and its own OpenMetrics parsing pass. The output directory is a drop-in
replacement for promtool's, and `promtool` remains a perfectly valid choice
if you'd rather not build a Go binary -- it's just slower.

So, of the three ways to get weewx archive data into Grafana Cloud, all
three are meant to stay available and are documented as such:

1. **Direct Remote Write push** (`grafana_backfill.py`'s default) -- fastest,
   but limited to samples inside Mimir's out-of-order ingestion window.
2. **`--output-file` (OpenMetrics text)** -- for archival, or as the input to
   whichever block-creation tool you choose.
3. **TSDB block upload** (this tool, or `promtool`, both feeding
   `mimirtool backfill`) -- for the history that's too old for (1).

## Why this exists as a separate Go tool rather than in Python

The on-disk TSDB block format (Snappy/XOR-encoded chunks plus a custom
indexed, checksummed binary index) has no maintained Python implementation --
it's only implemented in Go, inside `prometheus/prometheus`. There is no way
to do this natively in the Python weewx extension; see the discussion in the
project's git history / commit messages around this file's introduction.

## Does weewx have a standard way to bundle something like this?

No. Researched before writing this tool: weewx's `ExtensionInstaller`
(`weecfg.extension.ExtensionInstaller`, used by this project's `install.py`)
copies whatever's listed in its `files=[...]` manifest into paths under
`$WEEWX_ROOT` (typically `bin/user/`) via `shutil.copy`. That mechanism has
no build step -- it only copies files that already exist in the extension
package. There's no first-class convention in weewx for "here's a binary,
compile it for the target platform during install." Bundling a *compiled*
binary through the installer would mean shipping prebuilt binaries for every
platform/architecture, checked into the repo, which brings its own
supply-chain and staleness concerns.

That's why this tool intentionally lives outside `bin/user/` and is **not**
referenced from `install.py`: it's an optional, separately-built companion
for people doing a large one-time backfill, not something the extension
installs for every user. If it graduates out of prototype status, wiring it
in would mean either (a) documenting it as a manual companion tool (as this
README does today), or (b) publishing prebuilt release binaries and having
`grafana_backfill.py` shell out to one if present -- but that's future work,
not something this prototype takes on.

## Requirements

- A Go toolchain. Developed and tested against `go1.26.5`; anything
  reasonably recent (go1.23+) is likely fine -- if `go build` complains about
  the `go` directive in `go.mod` being too new for your installed toolchain,
  lower it with `go mod edit -go=1.23` (or your version) and re-run
  `go build`.
- Network access the first time you build, to download
  `github.com/prometheus/prometheus` and its dependencies via the Go module
  proxy (`go env GOPROXY`, `proxy.golang.org` by default).
- **Heads up on dependency size**: `github.com/prometheus/prometheus` is a
  monorepo containing the whole Prometheus server, not just the `tsdb`
  package this tool actually uses. `go mod tidy`/`go build` will pull in
  roughly 90 modules, including unrelated cloud SDKs (AWS, Azure, GCP) and
  `k8s.io/client-go`, because those are imported elsewhere in that module
  even though this tool never calls into them. This is expected and
  harmless -- unused code doesn't end up in the compiled binary (it's about
  28 MB, statically linked) -- but the first build will download noticeably
  more than the tool's own source size, and may take a minute or two
  depending on your connection.

## Building

```
cd tools/tsdb-block-writer
go build -o tsdb-block-writer .
```

This produces a single self-contained binary in the current directory
(`tsdb-block-writer`, or `tsdb-block-writer.exe` on Windows). Nothing needs
to be installed system-wide; `go.sum` pins the exact dependency versions
this was built and tested against.

## Usage

```
./tsdb-block-writer -input weewx_backfill.prom -output ./blocks -block-duration 168h
```

| Flag | Default | Meaning |
|---|---|---|
| `-input` | *(required)* | Path to an OpenMetrics text file, e.g. from `grafana_backfill.py --dry-run --output-file weewx_backfill.prom`. Every sample line must carry an explicit timestamp (the OpenMetrics writer in this project always includes one). |
| `-output` | `./tsdb_output` | Directory to write blocks into. Created if it doesn't exist. Each run adds new ULID-named block subdirectories; it does not touch or remove anything already there. |
| `-block-duration` | `2h` | Time span covered by each generated block. Samples are bucketed into consecutive, non-overlapping windows of this size, aligned so the first window starts at or before the earliest sample. |

Once it finishes, `-output`'s directory is exactly the kind of directory
`promtool tsdb create-blocks-from` would have produced -- point `mimirtool
backfill` at it the same way:

```
mimirtool backfill --address=<mimir-url> --id=<tenant-id> --user=<tenant-id> --key=<api-key> ./blocks/*
```

`<mimir-url>` must **not** include the `/api/prom` (or any other) suffix --
the block-upload API lives at the server root. `--id` alone only sets the
tenant, it isn't authentication: without `--user`/`--key` (HTTP Basic Auth;
`--user` is the same tenant/instance ID, `--key` an access-policy token with
`metrics: write`) the request fails to route at all. See the main
[README.md](../../README.md)'s "Getting an OpenMetrics file instead" section
for the full picture, including a Grafana-Cloud-specific gotcha: block
upload is disabled per-tenant by default and can only be enabled by Grafana
support -- no access-policy scope or client-side change works around it.

### Choosing `-block-duration`

Fewer, larger blocks are faster to build -- most of the per-block cost is
fixed overhead (index/chunk file writing, checksums), not proportional to
how much data is in it. Benchmarked on this project's real archive (a
Davis Vantage Pro2 station, ~32 metrics, 30-minute archive interval):

| Input | `-block-duration` | Blocks | Wall time |
|---|---|---|---|
| 90 days, 134,263 samples | `24h` | 90 | 14.8s |
| 90 days, 134,263 samples | `168h` (7 days) | 14 | 2.4s |
| ~7.1 years, 3,305,008 samples | `168h` (7 days) | 371 | 64.6s |
| 90 days, 134,263 samples | `24h`, via `promtool tsdb create-blocks-from` | 120 | 27.8s |

For comparison, `promtool tsdb create-blocks-from --max-block-duration=24h`
on the same 90-day input took 27.8s to this tool's 2.4s at `-block-duration
168h` -- roughly an order of magnitude, and the gap widens with larger
inputs since promtool's own per-block overhead dominates at scale. If you
plan to send the result to `mimirtool backfill`, check its (or your Mimir
tenant's) documented limits on maximum block size before picking a very
large duration; `168h` (7 days) was not observed to cause any problem in
these tests, but was not tested against a live backfill endpoint either
(see Verification below).

## Verification performed

No unit tests exist yet (prototype). Correctness was instead checked by
round-tripping real data: for each of a 3-day, 90-day, and the full ~7.1-year
archive OpenMetrics export from this project's `grafana_backfill.py
--dry-run --output-file`, the generated blocks were dumped back to
OpenMetrics text with `promtool tsdb dump-openmetrics` and compared against
the original input as a set of (metric name, sorted label pairs, value,
timestamp) tuples. All three runs matched **exactly** -- zero samples
missing, zero extra, zero altered, including at full scale (3,305,008
samples). `promtool tsdb analyze` was also run against individual generated
blocks and reported sane series/label-churn statistics with no corruption
warnings.

What was *not* tested: an actual `mimirtool backfill` push to a live Mimir/
Grafana Cloud endpoint. The block format is verified structurally sound and
readable by promtool's own tooling, but "mimirtool accepts it" is one step
further that needs a real backfill run against your stack to confirm.

## Known limitations

- **In-memory**: the entire input is parsed into memory (one time-sorted
  slice per series) before any block is written. For this project's ~3.3
  million-sample, 7-year archive that was fine, but a much larger archive
  could use proportionally more memory.
- **Gauges only**: only plain float samples are handled (`textparse`'s
  `EntrySeries`), matching what `grafana_metrics.write_openmetrics()` ever
  emits. Native histograms, counters-with-`_total` semantics, exemplars,
  etc. are not read or written.
- **No tests, no CI.** Confidence comes from the manual verification above,
  not an automated regression suite.
