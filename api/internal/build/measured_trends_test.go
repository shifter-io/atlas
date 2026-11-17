package build

import (
	"math"
	"testing"

	"github.com/Zeus-Development/atlas/api/internal/snap"
)

var (
	trendPulses = snap.Latency{P50: 300, P90: 700, P99: 1500}
	trendDaily  = snap.Latency{P50: 900, P90: 1800, P99: 4000}
	trendNone   = snap.Latency{P50: nan, P90: nan, P99: nan}
)

// trendWindow is a window from start to 2026-10-01T12:00 whose latency
// target has the pulses' latency and under (the daily tests', or none), with
// one series row per at: the pulses' latency and the daily tests' at daily
// rows, none at the others.
func trendWindow(start string, under snap.Latency, at []string, daily map[string]bool) snap.Window {
	w := figuresWindow(start)
	m := snap.Metrics{TimeSlots: &snap.TimeSlots{LatencyTargets: []string{latencyTarget}}, LatencyMs: trendPulses, LatencyUnderLoadMs: &under}
	w.Metrics = m
	w.Targets = map[string]snap.TargetResult{latencyTarget: {Metrics: m}}
	for _, a := range at {
		row := snap.SeriesRow{At: a, SuccessRate: 0.9, LatencyMs: trendPulses, LatencyUnderLoadMs: &trendNone}
		if daily[a] {
			row.LatencyUnderLoadMs = &trendDaily
		}
		w.Series = append(w.Series, row)
	}
	return w
}

func checkHistory(t *testing.T, name string, p *snap.Product, rangeID string, want []Num) {
	t.Helper()
	got := measuredHistory(p, rangeID, true)
	if len(got) != len(want) {
		t.Fatalf("%s: %d points, want %d", name, len(got), len(want))
	}
	for i, point := range got {
		if point.value != want[i] {
			t.Errorf("%s: point %d is %v, want %v", name, i, point.value, want[i])
		}
	}
}

// The latency history follows the headline, the latency of normal requests
// (the hourly pulses, HEADLINE-PULSE-1), never the daily tests' latency under
// load, whether or not the window has one.
func TestMeasuredLatencyFollowsTheHeadline(t *testing.T) {
	t.Parallel()
	at := []string{"2026-10-01T10:00:00Z", "2026-10-01T11:00:00Z"}
	daily := map[string]bool{at[0]: true}
	for _, c := range []struct {
		name  string
		under snap.Latency
		want  []Num
	}{
		{"daily tests and pulses", trendDaily, []Num{300, 300}},
		{"pulses only", trendNone, []Num{300, 300}},
	} {
		p := &snap.Product{Windows: map[string]snap.Window{"24h": trendWindow("2026-09-30T12:00:00Z", c.under, at, daily)}}
		checkHistory(t, c.name, p, "24h", c.want)
	}
}

// A 30d range under load over a 24h window with pulses only keeps the
// pulses' latency throughout: the 24h window's points replace the coarser
// ones they cover.
func TestMeasuredLatencyIsThePulsesAcrossWindows(t *testing.T) {
	t.Parallel()
	month := []string{"2026-09-10T00:00:00Z", "2026-09-20T00:00:00Z", "2026-10-01T06:00:00Z"}
	day := []string{"2026-10-01T06:00:00Z", "2026-10-01T10:00:00Z"}
	p := &snap.Product{Windows: map[string]snap.Window{
		"30d": trendWindow("2026-09-01T12:00:00Z", trendDaily, month, map[string]bool{month[0]: true, month[1]: true, month[2]: true}),
		"24h": trendWindow("2026-09-30T12:00:00Z", trendNone, day, nil),
	}}
	checkHistory(t, "30d under load", p, "30d", []Num{300, 300, 300, 300})
	checkHistory(t, "24h pulses only", p, "24h", []Num{300, 300})
}

// CHANGELOG-REAL-1 (#1314): the success change is dated at the end of the
// newest point it compares, at most the window's end: a 24h row of the
// calculator's is a UTC hour (SERIES-HOURLY-DAILY-1), one of a snapshot from
// before it five minutes; a daily row's end is 24 hours after its start; no
// time without two points. Until #1314's follow-up an hourly row was taken
// for five minutes: staging's success entries read 01:05 for the hour to 02:00.
func TestTheSuccessChangeIsDatedAtItsNewestPoint(t *testing.T) {
	t.Parallel()
	month := []string{"2026-09-10T00:00:00Z", "2026-09-20T00:00:00Z", "2026-09-29T00:00:00Z"}
	product := func(day ...string) *snap.Product {
		return &snap.Product{Windows: map[string]snap.Window{
			"30d": trendWindow("2026-09-01T12:00:00Z", trendNone, month, nil),
			"24h": trendWindow("2026-09-30T12:00:00Z", trendNone, day, nil),
		}}
	}
	for _, c := range []struct {
		name string
		day  []string
		want string
	}{
		{"hourly rows", []string{"2026-10-01T06:00:00Z", "2026-10-01T09:00:00Z"}, "2026-10-01T10:00:00Z"},
		{"five-minute rows", []string{"2026-10-01T09:00:00Z", "2026-10-01T09:05:00Z"}, "2026-10-01T09:10:00Z"},
		{"the current hour", []string{"2026-10-01T10:00:00Z", "2026-10-01T11:00:00Z"}, "2026-10-01T12:00:00Z"},
		{"a row off the hour: five minutes", []string{"2026-10-01T10:00:00Z", "2026-10-01T11:30:00Z"}, "2026-10-01T11:35:00Z"},
	} {
		for _, rangeID := range []string{"30d", "24h"} {
			if _, _, at := measuredTrend(product(c.day...), rangeID); at != snap.Ms(c.want) {
				t.Errorf("%s, %s: dated %s, want %s", c.name, rangeID, isoText(at), c.want)
			}
		}
	}
	p := product("2026-10-01T06:00:00Z")
	delete(p.Windows, "24h")
	if _, _, at := measuredTrend(p, "30d"); at != snap.Ms("2026-09-30T00:00:00Z") {
		t.Errorf("daily rows only: dated %s, want the newest row's end 2026-09-30", isoText(at))
	}
	p.Windows["30d"] = trendWindow("2026-09-01T12:00:00Z", trendNone, []string{"2026-09-30T00:00:00Z", "2026-10-01T00:00:00Z"}, nil)
	if _, _, at := measuredTrend(p, "30d"); at != snap.Ms("2026-10-01T12:00:00Z") {
		t.Errorf("today's partial day: dated %s, want the window's end 12:00", isoText(at))
	}
	p.Windows["30d"] = trendWindow("2026-09-01T12:00:00Z", trendNone, month[:1], nil)
	if d, _, at := measuredTrend(p, "30d"); d.OK() || !math.IsNaN(at) {
		t.Errorf("one point: %v dated %v, want no change and no time", d, at)
	}
}

// isoText is a time in ms as text, for messages.
func isoText(ms float64) string {
	if s := isoMs(ms); s != nil {
		return *s
	}
	return "none"
}
