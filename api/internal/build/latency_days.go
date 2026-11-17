package build

import (
	"time"

	"github.com/Zeus-Development/atlas/api/internal/jsnum"
	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/snap"
)

// The reasons of a null latencyDeltaPct (LATENCY-DELTA-24H-1,
// LATENCY-DELTA-PERIOD-1), the calculator's own codes
// (calculated/latency_days.py). Since LATENCY-DELTA-FALLBACK-1 every range
// falls back to the day rule, so a new calculation gives the day rule's.
const (
	// NANeedsTwoDays: the network has no two whole days of daily tests yet.
	NANeedsTwoDays = "needs_two_days"
	// NANeedsTwoPeriods: the same for the 7d and 30d ranges' two periods of
	// 7 or 30 whole days, only in a snapshot from before the fallback.
	NANeedsTwoPeriods = "needs_two_periods"
	// NADayNotCovered: a day or period compared has too few tested countries
	// or successes.
	NADayNotCovered = "day_not_covered"
)

// The latencyDeltaBasis codes (LATENCY-DELTA-FALLBACK-1): the periods a
// latencyDeltaPct compares, by their length in whole days.
const (
	DeltaBasisDay   = "day"
	DeltaBasisWeek  = "week"
	DeltaBasisMonth = "month"
)

var deltaBases = map[float64]string{1: DeltaBasisDay, 7: DeltaBasisWeek, 30: DeltaBasisMonth}

const dayMs = 24 * 60 * 60 * 1000

// latencyDelta is a measured range's latency change (latencyChange).
type latencyDelta struct {
	pct   Num        // latencyDeltaPct, at 1 dp
	edges trendEdges // the two periods' headline p50s, the changelog's values
	basis model.Str  // latencyDeltaBasis: day, week or month
	asOf  model.Str  // latencyDeltaAsOf: the last UTC day the periods cover
	why   string     // the reason code of a null pct ("" for the figure's general one)
	// carried: an earlier build's value kept on, which makes no changelog entry.
	carried bool
	at      float64 // the end (ms) of the newer period: the changelog entry's time
}

// latencyChange is a measured range's latency change: of the last 30, 7 or 1
// whole UTC days against as many before them, the longest the network's data
// allows up to the range, or the last one computed (dayLatencyDelta).
func latencyChange(sp *snap.Product, rangeID string) latencyDelta {
	return dayLatencyDelta(sp.Window(rangeID))
}

// dayLatencyDelta is the calculator's latency change of two periods of whole
// days (snap.LatencyDays) at 1 dp, with the two periods' headline p50s, their
// basis code and last day (LATENCY-ALWAYS-1: a carried value's is an earlier
// day), and its reason when null: the calculator's, or not_published in a
// snapshot from before it.
func dayLatencyDelta(w snap.Window) latencyDelta {
	none := latencyDelta{pct: nan, edges: trendEdges{nan, nan}}
	d := w.LatencyDays
	if d == nil {
		none.why = NANotPublished
		return none
	}
	none.why = NAInsufficient
	if !d.Pct.OK() || len(d.Days) != 2 {
		if d.Reason != nil {
			switch *d.Reason {
			case NANeedsTwoDays, NANeedsTwoPeriods, NADayNotCovered:
				none.why = *d.Reason
			}
		}
		return none
	}
	last := d.Days[1]
	end := snap.Ms(last.End)
	basis, ok := deltaBases[(end-snap.Ms(last.Start))/dayMs]
	if !ok {
		return none
	}
	asOf := time.UnixMilli(int64(end) - dayMs).UTC().Format(time.DateOnly)
	return latencyDelta{pct: Num(jsnum.FixedNum(d.Pct.F(), 1)), edges: trendEdges{d.Days[0].P50, last.P50},
		basis: model.S(basis), asOf: model.S(asOf), carried: d.Carried, at: end}
}
