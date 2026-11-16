package snap

import "github.com/Zeus-Development/atlas/api/internal/jsnum"

// LatencyDays is the 24h window's latency_day_delta (LATENCY-DELTA-24H-1):
// the headline latency p50 of the last whole UTC day against the day before
// it, in percent (unrounded; positive: slower), made once a day at a fixed
// hour (ComputedAt). Pct is NaN when a day has too few tested countries or
// successes; Reason then says why ("needs_two_days": the network has no test
// before the earlier day; "day_not_covered"). Basis is "under_load" or
// "normal", the headline latency's; Days are the earlier and the last day.
type LatencyDays struct {
	Pct        Num          `json:"pct"`
	Reason     *string      `json:"reason"`
	Basis      string       `json:"basis"`
	ComputedAt string       `json:"computed_at"`
	Days       []LatencyDay `json:"days"`
}

func (v *LatencyDays) UnmarshalJSON(d []byte) error {
	type plain LatencyDays
	return jsnum.DecodeNA(d, (*plain)(v))
}

// LatencyDay is one day of LatencyDays: its period [Start, End), headline
// latency p50 and tested countries.
type LatencyDay struct {
	Start     string `json:"start"`
	End       string `json:"end"`
	P50       Num    `json:"p50"`
	Countries Num    `json:"countries"`
}

func (v *LatencyDay) UnmarshalJSON(d []byte) error {
	type plain LatencyDay
	return jsnum.DecodeNA(d, (*plain)(v))
}
