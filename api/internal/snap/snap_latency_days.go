package snap

import "github.com/shifter-io/atlas/api/internal/jsnum"

// LatencyDays is a window's latency_day_delta (LATENCY-DELTA-24H-1,
// LATENCY-DELTA-PERIOD-1): the headline latency p50 of the last 1, 7 or 30
// whole UTC days (24h, 7d, 30d) against as many before them, in percent
// (unrounded; positive: slower), made once a day at a fixed hour
// (ComputedAt). When those periods do not pass, the calculator compares the
// last 7 whole days (30d), then the last day (LATENCY-DELTA-FALLBACK-1): Days
// are the periods compared. Pct is NaN when even the day has too few tested
// countries or successes; Reason then says why ("needs_two_days": the
// network has no test before the earlier day; "day_not_covered"; a snapshot
// from before the fallback: 7d and 30d "needs_two_periods").
// Basis is "under_load" or "normal", the headline latency's; Days are the
// earlier and the last period. Carried: the calculator kept an earlier
// build's value, no pair of its own day giving one (LATENCY-ALWAYS-1).
type LatencyDays struct {
	Pct        Num          `json:"pct"`
	Reason     *string      `json:"reason"`
	Basis      string       `json:"basis"`
	Carried    bool         `json:"carried"`
	ComputedAt string       `json:"computed_at"`
	Days       []LatencyDay `json:"days"`
}

func (v *LatencyDays) UnmarshalJSON(d []byte) error {
	type plain LatencyDays
	return jsnum.DecodeNA(d, (*plain)(v))
}

// LatencyDay is one period (day) of LatencyDays: its period [Start, End), headline
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
