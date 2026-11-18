package build

import (
	"math"
	"slices"
	"testing"
	"time"

	"github.com/shifter-io/atlas/api/internal/model"
	"github.com/shifter-io/atlas/api/internal/snap"
)

// curveAt is the axis of rangeID at the as-of time asOf (RFC 3339).
func curveAt(t *testing.T, asOf, rangeID string) curveAxis {
	t.Helper()
	at, err := time.Parse(time.RFC3339, asOf)
	if err != nil {
		t.Fatal(err)
	}
	return newCurveAxis(at, true, rangeMeta(rangeID))
}

// point is a curve point covering span from start (RFC 3339).
func point(t *testing.T, start string, span time.Duration, rate, requests float64) curvePoint {
	t.Helper()
	at, err := time.Parse(time.RFC3339, start)
	if err != nil {
		t.Fatal(err)
	}
	return curvePoint{mid: at.Add(span / 2), rate: rate, weight: requests}
}

func nearRate(a Num, b float64) bool { return a.OK() && math.Abs(a.F()-b) < 1e-9 }

// 24h has 24 UTC hours, 7d and 30d 7 and 30 UTC days, the newest one partial:
// each time is its bucket's start in minutes before the as-of time, oldest
// first. No as-of time: no buckets.
func TestCurveAxisIsTheRangesUTCHoursOrDays(t *testing.T) {
	t.Parallel()
	for rangeID, want := range map[string][]Num{
		"24h": {23*60 + 10, 22*60 + 10, 10},
		"7d":  {6*1440 + 610, 5*1440 + 610, 610},
		"30d": {29*1440 + 610, 28*1440 + 610, 610},
	} {
		times := curveAt(t, "2026-11-17T10:10:00Z", rangeID).times()
		n := map[string]int{"24h": 24, "7d": 7, "30d": 30}[rangeID]
		checkTimes(t, rangeID, n, times)
		if len(times) != n || times[0] != want[0] || times[1] != want[1] || times[n-1] != want[2] {
			t.Errorf("%s: %d times %v", rangeID, len(times), times)
		}
	}
	if ax := newCurveAxis(time.Time{}, false, rangeMeta("24h")); len(ax.curve().Values) != 0 {
		t.Errorf("an axis without an as-of time has buckets")
	}
}

// An hour's value is its points' rates weighted by their requests (a heavy
// slot outweighs a light one: 0.9, where the plain mean is 0.5); an hour
// without requests is null, never 0; the current hour has what exists.
func TestCurveWeightsEachBucketByRequests(t *testing.T) {
	t.Parallel()
	ax := curveAt(t, "2026-11-17T10:10:00Z", "24h")
	five := 5 * time.Minute
	s := ax.curve([]curvePoint{
		point(t, "2026-11-17T08:00:00Z", five, 1, 900),
		point(t, "2026-11-17T08:55:00Z", five, 0, 100),
		point(t, "2026-11-17T10:05:00Z", five, 0.5, 10),
		point(t, "2026-11-17T09:00:00Z", five, 0.2, 0),
	})
	if !nearRate(s.Values[21], 0.9) || s.Values[22].OK() || !nearRate(s.Values[23], 0.5) {
		t.Errorf("08:00 %v (want 0.9), 09:00 %v (want null), 10:00 %v (want 0.5)", s.Values[21], s.Values[22], s.Values[23])
	}
	if s.Values[0].OK() || len(s.Values) != 24 || s.Times[23] != 10 || s.Times[21] != 130 {
		t.Errorf("values %v times %v", s.Values, s.Times)
	}
	// The fleet pools every network's requests: a big network's hour
	// outweighs a small one's.
	pooled := ax.curve([]curvePoint{point(t, "2026-11-17T08:10:00Z", five, 0.8, 300)}, []curvePoint{point(t, "2026-11-17T08:20:00Z", five, 0.4, 100)})
	if !nearRate(pooled.Values[21], 0.7) {
		t.Errorf("pooled hour %v, want 0.7", pooled.Values[21])
	}
}

// A point counts in the UTC hour or day holding its middle: a 5-minute slot
// at 23:55 in that day, one at 00:00 in the next; a 24-hour slot of the 7d
// window (from the window start, not a UTC day) in the day holding most of
// it. Points before the oldest bucket are left out.
func TestCurvePointsCountInTheBucketHoldingTheirMiddle(t *testing.T) {
	t.Parallel()
	ax := curveAt(t, "2026-11-17T10:10:00Z", "7d")
	day, five := 24*time.Hour, 5*time.Minute
	s := ax.curve([]curvePoint{
		point(t, "2026-11-16T23:55:00Z", five, 0.6, 1),
		point(t, "2026-11-17T00:00:00Z", five, 0.3, 1),
		point(t, "2026-11-12T10:10:00Z", day, 0.9, 50),
		point(t, "2026-11-13T14:00:00Z", day, 0.7, 50),
		point(t, "2026-11-10T10:10:00Z", day, 0.1, 50),
	})
	for i, w := range []float64{math.NaN(), 0.9, math.NaN(), 0.7, math.NaN(), 0.6, 0.3} {
		if math.IsNaN(w) && s.Values[i].OK() || !math.IsNaN(w) && !nearRate(s.Values[i], w) {
			t.Errorf("day %d: %v, want %v (all %v)", i, s.Values[i], w, s.Values)
		}
	}
}

// A network's curve is on the axis (one times list for every network); a
// network without history has an empty one. Without a snapshot product its
// window stats points count once each, at their minutes before the as-of
// time; with one, each series row counts its completed requests.
func TestNetworkCurveIsOnTheAxis(t *testing.T) {
	t.Parallel()
	ax := curveAt(t, "2026-11-17T10:10:00Z", "24h")
	if e := networkCurve(nil, ax); e.Values == nil || len(e.Values)+len(e.Times) != 0 {
		t.Errorf("no history: %+v", e)
	}
	ws := &WindowStats{Success: []model.SeriesPoint{{T: 125, V: 1}, {T: 120, V: 0}, {T: 5, V: nan}}}
	plain := networkCurve(entryCurvePoints(&entry{P: &model.Provider{PrimaryKind: "residential"}}, ws, "24h", ax.asOf), ax)
	if !nearRate(plain.Values[21], 0.5) || plain.Values[23].OK() || !slices.Equal(plain.Times, ax.times()) {
		t.Errorf("window stats curve %v", plain.Values)
	}
	w := snap.Window{Period: snap.Period{Start: "2026-11-16T10:10:00Z", End: "2026-11-17T10:10:00Z"}, Series: []snap.SeriesRow{
		{At: "2026-11-17T08:10:00Z", Completed: 900, SuccessRate: 1}, {At: "2026-11-17T08:15:00Z", Completed: 100, SuccessRate: 0},
	}}
	e := &entry{P: &model.Provider{PrimaryKind: "residential"}, Snap: map[string]*snap.Product{"residential": {Windows: map[string]snap.Window{"24h": w}}}}
	got := networkCurve(entryCurvePoints(e, &WindowStats{}, "24h", ax.asOf), ax)
	if !nearRate(got.Values[21], 0.9) {
		t.Errorf("snapshot curve %v, want 0.9 at 08:00", got.Values)
	}
	if asOf, ok := curveAsOf([]*entry{e}, &model.Dataset{}, "24h"); !ok || !asOf.Equal(ax.asOf) {
		t.Errorf("as-of %v %v", asOf, ok)
	}
	stamp := "2026-11-17T10:10:00Z"
	if asOf, ok := curveAsOf(nil, &model.Dataset{Snapshot: model.SnapshotMeta{AsOf: &stamp}}, "24h"); !ok || !asOf.Equal(ax.asOf) {
		t.Errorf("snapshot as-of %v %v", asOf, ok)
	}
	if _, ok := curveAsOf(nil, &model.Dataset{}, "24h"); ok {
		t.Errorf("an as-of time without one")
	}
}
