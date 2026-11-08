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

// Difficulty is [browser_difficulty] decoded from the settings, plus each
// group without one the highest of its sources' (policy.py
// browser_difficulty); a group with no weighed source gets none, and a
// methodology without the table gives nil.
func TestMethodologyDifficulty(t *testing.T) {
	var m Methodology
	if err := json.Unmarshal([]byte(`{"id":"m","settings":{"grades":{"A+":88,"D":0},
		"browser_difficulty":{"amazon_pdp":2,"ai_overview":4,"ai_gemini":3,"kept":1},
		"target_groups":{"ai_answers":["ai_overview","ai_gemini"],"kept":["ai_overview"],"empty":["unknown"]}}}`), &m); err != nil {
		t.Fatal(err)
	}
	got := m.Difficulty()
	want := map[string]float64{"amazon_pdp": 2, "ai_overview": 4, "ai_gemini": 3, "kept": 1, "ai_answers": 4}
	if !maps.Equal(got, want) || m.Settings.Grades["A+"] != 88 {
		t.Errorf("difficulty %v, grades %v", got, m.Settings.Grades)
	}
	if (Methodology{}).Difficulty() != nil {
		t.Error("a methodology without the table has a difficulty")
	}
}
