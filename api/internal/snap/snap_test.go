package snap

import (
	"encoding/json"
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
