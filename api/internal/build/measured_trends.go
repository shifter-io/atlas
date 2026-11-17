package build

import (
	"sort"
	"time"

	"github.com/Zeus-Development/atlas/api/internal/jsnum"
	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/snap"
)

type measuredPoint struct {
	at       time.Time
	value    Num
	requests Num           // the series row's completed requests
	span     time.Duration // the time the row covers (seriesStep)
}

// seriesStep is the span the curves give a series row of window id when they
// place it by its middle: 5 minutes in the 24h window, 24 hours in the others.
// Since SERIES-HOURLY-DAILY-1 (#1301) the calculator writes one row per UTC
// hour (24h) or day, starting on the hour or at midnight, so the middle stays
// in the row's own hour or day; a snapshot from before it has 5-minute rows
// and 24-hour rows from the window's start, which these spans place too.
func seriesStep(id string) time.Duration {
	if id == "24h" {
		return 5 * time.Minute
	}
	return 24 * time.Hour
}

// measuredHistory matches every-request.ts, using finer observed history where available.
func measuredHistory(product *snap.Product, rangeID string, latency bool) []measuredPoint {
	start, end, ok := measuredPeriod(product.Windows[rangeID])
	if !ok {
		return nil
	}
	points := []measuredPoint{}
	for _, id := range []string{"30d", "7d", "24h"} {
		w, ok := product.Windows[id]
		if !ok {
			continue
		}
		from, until, ok := measuredPeriod(w)
		if !ok || from.Before(start) || until.After(end) {
			continue
		}
		incoming := measuredPoints(w, start, end, latency)
		if len(incoming) == 0 {
			continue
		}
		for i := range incoming {
			incoming[i].span = seriesStep(id)
		}
		kept := []measuredPoint{}
		for _, row := range points {
			if row.at.Before(from) || !row.at.Before(until) {
				kept = append(kept, row)
			}
		}
		points = append(kept, incoming...)
	}
	sort.SliceStable(points, func(i, j int) bool { return points[i].at.Before(points[j].at) })
	return points
}

// windowHistory is the range window's own series rows with a value, oldest
// first: what the home's hourly and daily series read (CURVE-HOURLY-DAILY-1,
// #1301). Since SERIES-HOURLY-DAILY-1 the calculator writes one row per UTC
// hour (24h) or day, so no finer window is merged in as measuredHistory does:
// its hours would overlap the range's days.
func windowHistory(product *snap.Product, rangeID string, latency bool) []measuredPoint {
	w := product.Windows[rangeID]
	start, end, ok := measuredPeriod(w)
	if !ok {
		return nil
	}
	points := measuredPoints(w, start, end, latency)
	for i := range points {
		points[i].span = seriesStep(rangeID)
	}
	sort.SliceStable(points, func(i, j int) bool { return points[i].at.Before(points[j].at) })
	return points
}

// measuredPeriod is w's period, false when either end does not parse.
func measuredPeriod(w snap.Window) (start, end time.Time, ok bool) {
	start, err := time.Parse(time.RFC3339Nano, w.Period.Start)
	if err != nil {
		return start, end, false
	}
	end, err = time.Parse(time.RFC3339Nano, w.Period.End)
	return start, end, err == nil
}

// measuredPoints are w's series values (success rate or the headline
// latency, seriesLatency) at start <= at < end, those without a value left out.
func measuredPoints(w snap.Window, start, end time.Time, latency bool) []measuredPoint {
	points := []measuredPoint{}
	for _, row := range w.Series {
		at, err := time.Parse(time.RFC3339Nano, row.At)
		if err != nil || at.Before(start) || !at.Before(end) {
			continue
		}
		value := row.SuccessRate
		if latency {
			value = seriesLatency(w, row).P50
		}
		if value.OK() {
			points = append(points, measuredPoint{at: at, value: value, requests: row.Completed})
		}
	}
	return points
}

// measuredTrend is the success change: it compares opening/closing fifths
// without treating missing history as zero; it also gives the two means it
// compares (NaN without one). The latency change compares whole days
// (latencyChange).
func measuredTrend(product *snap.Product, rangeID string) (Num, trendEdges) {
	points := measuredHistory(product, rangeID, false)
	if len(points) < 2 {
		return nan, trendEdges{nan, nan}
	}
	series := make([]model.SeriesPoint, len(points))
	for i, p := range points {
		series[i] = model.SeriesPoint{V: p.value}
	}
	opening, closing := edgeMeans(series)
	edges := trendEdges{Num(opening), Num(closing)}
	return Num(jsnum.FixedNum(trendPts(series), 2)), edges
}
