package build

import (
	"math"
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

// seriesStep is the time a series row of window id covers, as the calculator
// buckets it (calculated/aggregate.py series): five minutes in the 24h
// window, 24 hours from the window's start in the others.
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
// compares (NaN without one), and the end (ms) of the newest point it
// compares. The latency change compares whole days (latencyChange).
func measuredTrend(product *snap.Product, rangeID string) (Num, trendEdges, float64) {
	points := measuredHistory(product, rangeID, false)
	if len(points) < 2 {
		return nan, trendEdges{nan, nan}, math.NaN()
	}
	series := make([]model.SeriesPoint, len(points))
	for i, p := range points {
		series[i] = model.SeriesPoint{V: p.value}
	}
	opening, closing := edgeMeans(series)
	edges := trendEdges{Num(opening), Num(closing)}
	newest := points[len(points)-1]
	at := float64(newest.at.Add(newest.span).UnixMilli())
	return Num(jsnum.FixedNum(trendPts(series), 2)), edges, at
}
