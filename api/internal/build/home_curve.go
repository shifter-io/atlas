package build

import (
	"time"

	"github.com/Zeus-Development/atlas/api/internal/model"
)

// The home's success score curves (CURVE-HOURLY-DAILY-1, #1301): one point
// per UTC hour for 24h (24 points) and per UTC day for 7d and 30d (7 and 30),
// the newest one partial, each the requests-weighted rate of the history
// points in it, Σ rate × requests ÷ Σ requests (so Σ successes ÷ Σ completed:
// each point is its series row's request ratio). A point counts in the hour
// or day that holds its middle: a 5-minute row's, or a 24-hour row of the 7d
// and 30d windows (24 hours from the window's start, not a UTC day). Points
// before the oldest bucket are left out; a bucket without requests is null.

// curvePoint is one success history point: the middle of the time it
// covers, its rate and its weight (its completed requests).
type curvePoint struct {
	mid          time.Time
	rate, weight float64
}

// curveAxis is a range's curve buckets: n of step each, the newest the one
// holding asOf.
type curveAxis struct {
	asOf time.Time
	step time.Duration
	n    int
}

// newCurveAxis is r's axis at asOf: 24 UTC hours for a one-day range, else
// r.Days UTC days. No buckets without an as-of time.
func newCurveAxis(asOf time.Time, ok bool, r RangeMeta) curveAxis {
	ax := curveAxis{asOf: asOf, step: 24 * time.Hour, n: r.Days}
	if r.Days == 1 {
		ax.step, ax.n = time.Hour, 24
	}
	if !ok {
		ax.n = 0
	}
	return ax
}

// newest is the start of the bucket holding the last instant before asOf (a
// UTC hour or day: Truncate counts from the zero time, a UTC midnight), so an
// as-of time on the hour or at midnight ends a full bucket, not an empty one.
func (ax curveAxis) newest() time.Time { return ax.asOf.Add(-time.Nanosecond).Truncate(ax.step) }

// start is bucket i's start, oldest first.
func (ax curveAxis) start(i int) time.Time {
	return ax.newest().Add(-time.Duration(ax.n-1-i) * ax.step)
}

// times is each bucket's start in minutes before asOf, oldest first.
func (ax curveAxis) times() []Num {
	out := make([]Num, ax.n)
	for i := range out {
		out[i] = Num(ax.asOf.Sub(ax.start(i)).Minutes())
	}
	return out
}

// index is the bucket holding t; -1 outside the axis.
func (ax curveAxis) index(t time.Time) int {
	back := ax.newest().Sub(t.Truncate(ax.step)) / ax.step
	if back < 0 || int(back) >= ax.n {
		return -1
	}
	return ax.n - 1 - int(back)
}

// rated is the points' rates on the axis, every list pooled (the fleet's
// networks): each bucket's requests-weighted rate and its requests; a NaN
// rate and 0 requests without requests.
func (ax curveAxis) rated(lists ...[]curvePoint) []ratedRate {
	sum, weight := make([]float64, ax.n), make([]float64, ax.n)
	for _, points := range lists {
		for _, p := range points {
			if i := ax.index(p.mid); i >= 0 && p.weight > 0 {
				sum[i], weight[i] = sum[i]+p.rate*p.weight, weight[i]+p.weight
			}
		}
	}
	out := make([]ratedRate, ax.n)
	for i := range out {
		out[i] = ratedRate{rate: nan, requests: Num(weight[i])}
		if weight[i] > 0 {
			out[i].rate = Num(sum[i] / weight[i])
		}
	}
	return out
}

// curve is rated's rates with the axis times.
func (ax curveAxis) curve(lists ...[]curvePoint) SparkSeries {
	s := SparkSeries{Values: make([]Num, ax.n), Times: ax.times()}
	for i, r := range ax.rated(lists...) {
		s.Values[i] = r.rate
	}
	return s
}

// curveAsOf is the range windows' end, the build's as-of time: the first
// fleet product's window end, else the snapshot's asOf; false without one.
func curveAsOf(fleet []*entry, ds *model.Dataset, rangeID string) (time.Time, bool) {
	for _, e := range fleet {
		if end, err := time.Parse(time.RFC3339Nano, e.Snap[e.P.PrimaryKind].Windows[rangeID].Period.End); err == nil {
			return end, true
		}
	}
	if ds.Snapshot.AsOf == nil {
		return time.Time{}, false
	}
	end, err := time.Parse(time.RFC3339Nano, *ds.Snapshot.AsOf)
	return end, err == nil
}

// entryCurvePoints is a provider's success history for the range as curve
// points: its headline snapshot product's measured history weighted by each
// row's completed requests, else its window stats series (no request counts:
// each point weighs 1, at its start: its own time before asOf).
func entryCurvePoints(e *entry, s *WindowStats, rangeID string, asOf time.Time) []curvePoint {
	out := []curvePoint{}
	product := e.Snap[e.P.PrimaryKind]
	if product == nil {
		for _, p := range s.Success {
			if p.T.OK() && p.V.OK() {
				out = append(out, curvePoint{mid: asOf.Add(-time.Duration(p.T.F() * float64(time.Minute))), rate: p.V.F(), weight: 1})
			}
		}
		return out
	}
	for _, p := range windowHistory(product, rangeID, false) {
		if p.requests.OK() {
			out = append(out, curvePoint{mid: p.at.Add(p.span / 2), rate: p.value.F(), weight: p.requests.F()})
		}
	}
	return out
}

// fleetCurve is the market's success curve: every fleet network's points
// pooled per bucket by their requests.
func fleetCurve(fleet []*entry, stats map[string]map[string]*WindowStats, ax curveAxis, rangeID string) SparkSeries {
	lists := make([][]curvePoint, 0, len(fleet))
	for _, e := range fleet {
		if s := stats[e.P.Slug][rangeID]; s != nil {
			lists = append(lists, entryCurvePoints(e, s, rangeID, ax.asOf))
		}
	}
	return ax.curve(lists...)
}

// networkCurve is a row's success curve on the axis; empty for a row without
// history, as before.
func networkCurve(points []curvePoint, ax curveAxis) SparkSeries {
	if len(points) == 0 {
		return SparkSeries{Values: []Num{}, Times: []Num{}}
	}
	return ax.curve(points)
}
