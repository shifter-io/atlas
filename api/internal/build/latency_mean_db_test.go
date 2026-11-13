package build_test

import (
	"bytes"
	"encoding/json"
	"os"
	"slices"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/build"
)

// everyRequestWithMean is the every-request fixture with mean set on each
// window's generic_http latency under load (the head's latency object) and a
// different light-load mean on its latency_ms, as the framework writes both
// since #161; a nil mean leaves the fixture as it is.
func everyRequestWithMean(t *testing.T, mean any) []byte {
	t.Helper()
	body, err := os.ReadFile(repoFile(t, "fixtures", "measured", "snapshot-every-request.json"))
	if err != nil || mean == nil {
		if err != nil {
			t.Fatal(err)
		}
		return body
	}
	var s map[string]any
	if err := json.Unmarshal(body, &s); err != nil {
		t.Fatal(err)
	}
	for _, w := range s["products"].([]any)[0].(map[string]any)["windows"].(map[string]any) {
		metrics := w.(map[string]any)["targets"].(map[string]any)["generic_http"].(map[string]any)["metrics"].(map[string]any)
		metrics["latency_under_load_ms"].(map[string]any)["mean"] = mean
		metrics["latency_ms"].(map[string]any)["mean"] = 1031.4
	}
	out, err := json.Marshal(s)
	if err != nil {
		t.Fatal(err)
	}
	return out
}

// performance.latencyMean is the head's mean next to latencyP50 (LAT-MEAN-API),
// the under-load one and never the light-load latency_ms mean; a snapshot
// without the key gives null with latencyP50's N/A reason.
func TestTheHeadLatencyMeanIsServedAsLatencyMean(t *testing.T) {
	t.Parallel()
	for _, c := range []struct {
		name string
		mean any
		json string
	}{
		{"with mean", 1210.37, `"latencyP50":1454.08,"latencyMean":1210.37,"latencyP90":1770.54`},
		{"without mean", nil, `"latencyP50":1454.08,"latencyMean":null,"latencyP90":1770.54`},
	} {
		t.Run(c.name, func(t *testing.T) {
			res := publishedBuild(t, everyRequestWithMean(t, c.mean))
			p := res.Details["shifter"].Provider
			got, na := p.Performance.LatencyMean, slices.Contains(p.NA, "performance.latencyMean")
			if c.mean != nil && (got != Num(c.mean.(float64)) || na) || c.mean == nil && (got.OK() || !na) {
				t.Errorf("latencyMean %v, N/A %v", got, na)
			}
			// latencyP50's reason: no load test, from the snapshot.
			if f := res.Coverage.Fields["performance.latencyMean"]; c.mean == nil &&
				(f.Reason != build.NANoLoadTest || f.Source == nil || *f.Source != "snapshot") {
				t.Errorf("N/A reason %q, source %v", f.Reason, f.Source)
			}
			var file bytes.Buffer
			if err := build.WriteDataset(&file, res.Dataset); err != nil {
				t.Fatal(err)
			}
			if !bytes.Contains(file.Bytes(), []byte(c.json)) {
				t.Errorf("the dataset file has no %s", c.json)
			}
		})
	}
}
