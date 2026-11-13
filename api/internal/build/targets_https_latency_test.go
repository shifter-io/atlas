package build

import (
	"encoding/json"
	"testing"

	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/snap"
)

// httpsWindow is a window whose generic_https metrics are https (JSON), next
// to an every-request generic_http with light-load latency only.
func httpsWindow(t *testing.T, https string) snap.Window {
	t.Helper()
	var w snap.Window
	raw := `{"targets": {
 "generic_http": {"metrics": {"completed": 100, "success_rate": 0.9, "latency_ms": {"p50": 1000, "p90": 2000}, "time_slots": {"latency": 1}}},
 "generic_https": {"metrics": ` + https + `}}}`
	if err := json.Unmarshal([]byte(raw), &w); err != nil {
		t.Fatal(err)
	}
	return w
}

var httpsTargets = []model.TargetMeta{
	{ID: "generic_http", Source: model.SourceMeasured, Engine: model.S(model.EngineHTTP)},
	{ID: "generic_https", Source: model.SourceMeasured, Engine: model.S(model.EngineHTTP)},
}

// httpsCellNA is the cells of w and the reason of the https cell's null
// latencyP50 ("" when it has a value).
func httpsCellNA(t *testing.T, w snap.Window) (map[string]*TargetCell, string) {
	t.Helper()
	cells := targetCells(w, httpsTargets, 4)
	a := &TargetsAnswer{Networks: []TargetNetwork{{Slug: "a", Cells: cells,
		tried: map[string]bool{"generic_http": true, "generic_https": true}, price: 4}}}
	if err := a.listNA(""); err != nil {
		t.Fatal(err)
	}
	return cells, a.NAList.ReasonOf("networks[].cells.generic_https.latencyP50")
}

// HTTPS-LATENCY-1: an HTTP target other than the latency target shows its
// own display-only light-load latency; generic_http keeps its headline.
func TestAnHTTPSCellShowsItsOwnLatency(t *testing.T) {
	t.Parallel()
	cells, code := httpsCellNA(t, httpsWindow(t,
		`{"completed": 100, "success_rate": 0.8, "latency_ms": {"p50": 1250, "p90": 2500}, "latency_reason": null, "time_slots": {"latency": 1}}`))
	if c := cells["generic_https"]; c.LatencyP50 != 1250 || c.LatencyP90 != 2500 || code != "" {
		t.Errorf("https %+v, reason %q", c.NetworkCell, code)
	}
	if c := cells["generic_http"]; c.LatencyP50 != 1000 || c.LatencyP90 != 2000 {
		t.Errorf("http %+v", c.NetworkCell)
	}
}

// Below min_latency_successes the latency is null with few_successes; a
// snapshot written before (no latency for other targets) keeps not_measured,
// and so does one before every-request scoring, whatever latency it holds.
func TestAnHTTPSCellsNullLatencyHasItsReason(t *testing.T) {
	t.Parallel()
	for name, tc := range map[string]struct{ metrics, code string }{
		"few": {`{"completed": 10, "latency_ms": {"p50": null}, "latency_reason": "No pulse with enough successes in this window",
 "time_slots": {"latency": 0}}`, NAFewSuccesses},
		"before": {`{"completed": 10, "latency_ms": {"p50": null}, "latency_reason": "` + notLatencyTarget + `",
 "time_slots": {"latency": 0}}`, NANotMeasured},
		"older": {`{"completed": 10, "latency_ms": {"p50": 900, "p90": 1800}}`, NANotMeasured},
	} {
		cells, code := httpsCellNA(t, httpsWindow(t, tc.metrics))
		if c := cells["generic_https"]; c.LatencyP50.OK() || c.LatencyP90.OK() || code != tc.code {
			t.Errorf("%s: https %+v, reason %q", name, c.NetworkCell, code)
		}
	}
}

// The network answer's targets[] (and so the dataset file's) carry the same
// display latency; a browser target never has one.
func TestTargetBenchmarksShowTheDisplayLatency(t *testing.T) {
	t.Parallel()
	w := httpsWindow(t, `{"completed": 100, "latency_ms": {"p50": 1250, "p90": 2500}, "time_slots": {"latency": 1}}`)
	browser := model.TargetMeta{ID: "generic_https", Source: model.SourceMeasured, Engine: model.S(model.EngineBrowser)}
	got := targetBenchmarks(w, append(httpsTargets, browser))
	if got[1].LatencyP50 != 1250 || got[1].LatencyP90 != 2500 || got[0].LatencyP50 != 1000 {
		t.Errorf("benchmarks %+v", got[:2])
	}
	if got[2].LatencyP50.OK() {
		t.Errorf("browser %+v", got[2])
	}
}
