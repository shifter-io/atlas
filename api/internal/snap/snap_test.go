package snap

import (
	"encoding/json"
	"maps"
	"testing"
)

// A v4 head's latency object may carry the mean (LAT-MEAN-API, the
// framework's #161); a head without it, or with a null mean, decodes as
// NaN, never as 0.
func TestTheHeadLatencyMeanIsOptional(t *testing.T) {
	for _, c := range []struct {
		name, latency string
		mean          float64 // 0: want NaN
	}{
		{"with mean", `{"mean":1210.37,"p50":1113,"p90":1395,"p95":1700,"p99":3292}`, 1210.37},
		{"without mean", `{"p50":1113,"p90":1395,"p95":1700,"p99":3292}`, 0},
		{"null mean", `{"mean":null,"p50":1113,"p90":1395,"p95":1700,"p99":3292}`, 0},
	} {
		var m Metrics
		head := `{"time_slots":{},"latency_ms":{"p50":980},"latency_under_load_ms":` + c.latency + `}`
		if err := json.Unmarshal([]byte(head), &m); err != nil {
			t.Fatalf("%s: %v", c.name, err)
		}
		if m.LatencyUnderLoadMs == nil || m.LatencyUnderLoadMs.P50 != 1113 {
			t.Fatalf("%s: latency under load %+v", c.name, m.LatencyUnderLoadMs)
		}
		got := m.LatencyUnderLoadMs.Mean
		if c.mean == 0 && got.OK() || c.mean != 0 && got != Num(c.mean) {
			t.Errorf("%s: mean %v, want %v (0 means null)", c.name, got, c.mean)
		}
		if m.LatencyMs.Mean.OK() {
			t.Errorf("%s: light-load latency without a mean decodes as %v", c.name, m.LatencyMs.Mean)
		}
	}
}

// The settings' [grades] decode by label; a methodology without them has nil.
func TestMethodologyGrades(t *testing.T) {
	var m Methodology
	if err := json.Unmarshal([]byte(`{"id":"m","settings":{"grades":{"A+":88,"B":64,"D":0}}}`), &m); err != nil {
		t.Fatal(err)
	}
	if want := map[string]float64{"A+": 88, "B": 64, "D": 0}; !maps.Equal(m.Settings.Grades, want) {
		t.Errorf("grades %v", m.Settings.Grades)
	}
	if (Methodology{}).Settings.Grades != nil {
		t.Error("a methodology without grades has some")
	}
}

// success_overall decodes with its targets as scores by target id, or as a
// list of target ids (browser_overall's form), each then without a score.
func TestSuccessOverallDecodesTargetsAsAMapOrAList(t *testing.T) {
	var w Window
	if err := json.Unmarshal([]byte(`{"success_overall":{"score":7,"rank":2,"ranked_of":4,"status":"ranked",`+
		`"targets":{"generic_http":7},"weights":{"generic_http":1}}}`), &w); err != nil {
		t.Fatal(err)
	}
	if o := w.SuccessOverall; o == nil || o.Score != 7 || o.RankedOf != 4 || o.Targets["generic_http"] != 7 || o.Weights["generic_http"] != 1 {
		t.Fatalf("map form %+v", w.SuccessOverall)
	}
	var listed Window
	if err := json.Unmarshal([]byte(`{"success_overall":{"score":null,"rank":null,"ranked_of":null,`+
		`"status":"no_ranked_targets","targets":["generic_http"],"weights":{}}}`), &listed); err != nil {
		t.Fatal(err)
	}
	o := listed.SuccessOverall
	if o == nil || o.Score.OK() || o.Status != "no_ranked_targets" || len(o.Targets) != 1 || o.Targets["generic_http"].OK() {
		t.Fatalf("list form %+v", o)
	}
	if err := json.Unmarshal([]byte(`{"success_overall":{"targets":7}}`), &listed); err == nil {
		t.Fatal("a number as targets decoded")
	}
}
