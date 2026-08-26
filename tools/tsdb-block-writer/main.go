package main

import (
	"context"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"os"
	"sort"
	"time"

	"github.com/prometheus/prometheus/model/labels"
	"github.com/prometheus/prometheus/model/textparse"
	"github.com/prometheus/prometheus/storage"
	"github.com/prometheus/prometheus/tsdb"
)

type sample struct {
	t int64
	v float64
}

type seriesAcc struct {
	lbls    labels.Labels
	samples []sample
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		os.Exit(1)
	}
}

func run() error {
	inputPath := flag.String("input", "", "path to an OpenMetrics text file (e.g. grafana_backfill.py --output-file)")
	outputDir := flag.String("output", "./tsdb_output", "directory to write TSDB blocks into")
	blockDuration := flag.Duration("block-duration", 2*time.Hour, "time span covered by each generated block")
	flag.Parse()

	if *inputPath == "" {
		return fmt.Errorf("-input is required")
	}
	if *blockDuration <= 0 {
		return fmt.Errorf("-block-duration must be positive")
	}

	data, err := os.ReadFile(*inputPath)
	if err != nil {
		return fmt.Errorf("reading %s: %w", *inputPath, err)
	}

	series, err := parseOpenMetrics(data)
	if err != nil {
		return fmt.Errorf("parsing %s: %w", *inputPath, err)
	}
	if len(series) == 0 {
		return fmt.Errorf("no samples found in %s", *inputPath)
	}

	if err := os.MkdirAll(*outputDir, 0o755); err != nil {
		return fmt.Errorf("creating %s: %w", *outputDir, err)
	}

	nBlocks, nSamples, err := writeBlocks(series, *outputDir, blockDuration.Milliseconds())
	if err != nil {
		return err
	}
	fmt.Printf("wrote %d samples across %d series into %d block(s) in %s\n",
		nSamples, len(series), nBlocks, *outputDir)
	return nil
}

// parseOpenMetrics reads every EntrySeries sample from an OpenMetrics text
// file and groups them by their label set, sorted ascending by timestamp --
// the order TSDB requires within a series.
func parseOpenMetrics(data []byte) (map[string]*seriesAcc, error) {
	st := labels.NewSymbolTable()
	p := textparse.NewOpenMetricsParser(data, st)
	series := make(map[string]*seriesAcc)
	for {
		entry, err := p.Next()
		if err != nil {
			if err == io.EOF {
				break
			}
			return nil, err
		}
		if entry != textparse.EntrySeries {
			continue
		}
		_, ts, val := p.Series()
		if ts == nil {
			return nil, fmt.Errorf("sample has no timestamp; this tool only accepts " +
				"OpenMetrics text with an explicit timestamp on every line")
		}
		var lbls labels.Labels
		p.Labels(&lbls)
		key := lbls.String()
		acc, ok := series[key]
		if !ok {
			acc = &seriesAcc{lbls: lbls.Copy()}
			series[key] = acc
		}
		acc.samples = append(acc.samples, sample{t: *ts, v: val})
	}
	for _, acc := range series {
		sort.Slice(acc.samples, func(i, j int) bool { return acc.samples[i].t < acc.samples[j].t })
	}
	return series, nil
}

// writeBlocks buckets every series' samples into consecutive blockDurationMs
// windows and writes one TSDB block per non-empty window via
// tsdb.NewBlockWriter, exactly what "promtool tsdb create-blocks-from
// openmetrics" does internally -- but driven straight from Go instead of
// round-tripping through promtool's CLI and its own OpenMetrics parsing pass.
func writeBlocks(series map[string]*seriesAcc, outputDir string, blockDurationMs int64) (int, int, error) {
	minTs, maxTs := int64(0), int64(0)
	haveAny := false
	for _, acc := range series {
		if len(acc.samples) == 0 {
			continue
		}
		lo, hi := acc.samples[0].t, acc.samples[len(acc.samples)-1].t
		if !haveAny || lo < minTs {
			minTs = lo
		}
		if !haveAny || hi > maxTs {
			maxTs = hi
		}
		haveAny = true
	}
	if !haveAny {
		return 0, 0, fmt.Errorf("no samples to write")
	}

	logger := slog.New(slog.NewTextHandler(os.Stderr, nil))
	ctx := context.Background()

	// Per-series read cursor into its (already time-sorted) sample slice.
	// Samples are consumed strictly left-to-right across increasing buckets,
	// so a single forward-only index per series is all that's needed.
	cursor := make(map[string]int, len(series))
	bucketStart := minTs - floorMod(minTs, blockDurationMs)

	nBlocks, nSamples := 0, 0
	for bucketStart <= maxTs {
		bucketEnd := bucketStart + blockDurationMs

		// Gather every sample due in this bucket across all series, then
		// append them in one global ascending-timestamp order. This matters
		// because Head.Appender() anchors its valid-append-time window to
		// whichever sample is appended *first* in the transaction (map
		// iteration order is otherwise random); appending out of global
		// order can make an unrelated series' legitimately-earlier sample
		// land before that anchor and get rejected as "out of bounds".
		type pendingSample struct {
			key string
			acc *seriesAcc
			idx int
		}
		var pending []pendingSample
		for key, acc := range series {
			i := cursor[key]
			for i < len(acc.samples) && acc.samples[i].t < bucketEnd {
				pending = append(pending, pendingSample{key: key, acc: acc, idx: i})
				i++
			}
			cursor[key] = i
		}
		sort.Slice(pending, func(i, j int) bool {
			return pending[i].acc.samples[pending[i].idx].t < pending[j].acc.samples[pending[j].idx].t
		})

		w, err := tsdb.NewBlockWriter(logger, outputDir, blockDurationMs)
		if err != nil {
			return nBlocks, nSamples, fmt.Errorf("creating block writer: %w", err)
		}
		app := w.Appender(ctx)

		refs := make(map[string]storage.SeriesRef, len(series))
		wrote := 0
		for _, ps := range pending {
			s := ps.acc.samples[ps.idx]
			ref, err := app.Append(refs[ps.key], ps.acc.lbls, s.t, s.v)
			if err != nil {
				return nBlocks, nSamples, fmt.Errorf(
					"appending sample for %s: %w", ps.acc.lbls, err)
			}
			refs[ps.key] = ref
			wrote++
		}

		if wrote == 0 {
			if err := app.Rollback(); err != nil {
				return nBlocks, nSamples, fmt.Errorf("rolling back empty bucket: %w", err)
			}
			if err := w.Close(); err != nil {
				return nBlocks, nSamples, err
			}
			bucketStart = bucketEnd
			continue
		}

		if err := app.Commit(); err != nil {
			return nBlocks, nSamples, fmt.Errorf("committing block: %w", err)
		}
		blockID, err := w.Flush(ctx)
		if err != nil {
			return nBlocks, nSamples, fmt.Errorf("flushing block: %w", err)
		}
		if err := w.Close(); err != nil {
			return nBlocks, nSamples, err
		}

		nBlocks++
		nSamples += wrote
		fmt.Printf("block %s: %d samples, [%s, %s)\n",
			blockID, wrote,
			time.UnixMilli(bucketStart).UTC().Format(time.RFC3339),
			time.UnixMilli(bucketEnd).UTC().Format(time.RFC3339))

		bucketStart = bucketEnd
	}

	return nBlocks, nSamples, nil
}

// floorMod returns the non-negative remainder of t/m, so t-floorMod(t,m) is
// always the aligned bucket boundary at or before t.
func floorMod(t, m int64) int64 {
	r := t % m
	if r < 0 {
		r += m
	}
	return r
}
