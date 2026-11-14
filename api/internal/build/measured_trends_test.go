package build

import (
	"testing"

	"github.com/Zeus-Development/atlas/api/internal/snap"
)

// The latency history follows the headline (LATENCY-LABELS-1, #1075): under
// load where the window has the daily tests' latency under load, else the
// latency of normal requests (the hourly pulses), never N/A for want of a
// load test.
func TestMeasuredLatencyFollowsTheHeadline(t *testing.T) {
	t.Parallel()
	pulses := snap.Latency{P50: 300, P90: 700, P99: 1500}
	daily := snap.Latency{P50: 900, P90: 1800, P99: 4000}
	none := snap.Latency{P50: nan, P90: nan, P99: nan}
	window := func(under snap.Latency) snap.Window {
		w := figuresWindow("2026-09-30T12:00:00Z")
		m := snap.Metrics{TimeSlots: &snap.TimeSlots{LatencyTargets: []string{latencyTarget}}, LatencyMs: pulses, LatencyUnderLoadMs: &under}
		w.Metrics = m
		w.Targets = map[string]snap.TargetResult{latencyTarget: {Metrics: m}}
		w.Series = []snap.SeriesRow{
			{At: "2026-10-01T10:00:00Z", SuccessRate: 0.9, LatencyMs: pulses, LatencyUnderLoadMs: &daily},
			{At: "2026-10-01T11:00:00Z", SuccessRate: 0.9, LatencyMs: pulses, LatencyUnderLoadMs: &none},
		}
		return w
	}
	for _, c := range []struct {
		name  string
		under snap.Latency
		want  []Num
	}{
		{"daily tests and pulses", daily, []Num{900}},
		{"pulses only", none, []Num{300, 300}},
	} {
		p := &snap.Product{Windows: map[string]snap.Window{"24h": window(c.under)}}
		got := measuredHistory(p, "24h", true)
		if len(got) != len(c.want) {
			t.Fatalf("%s: %d points, want %d", c.name, len(got), len(c.want))
		}
		for i, point := range got {
			if point.value != c.want[i] {
				t.Errorf("%s: point %d is %v, want %v", c.name, i, point.value, c.want[i])
			}
		}
	}
}
