package build

import (
	"math"
	"sort"
	"time"

	"github.com/Zeus-Development/proxy-index/api/internal/jsnum"
	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/Zeus-Development/proxy-index/api/internal/snap"
)

type measuredPoint struct {
	at       time.Time
	value    Num
	requests Num // the series row's completed requests
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

// measuredPoints are w's series values (success rate or latency) at
// start <= at < end, those without a value left out.
func measuredPoints(w snap.Window, start, end time.Time, latency bool) []measuredPoint {
	points := []measuredPoint{}
	for _, row := range w.Series {
		at, err := time.Parse(time.RFC3339Nano, row.At)
		if err != nil || at.Before(start) || !at.Before(end) {
			continue
		}
		value := row.SuccessRate
		if latency {
			value = measuredLatency(w, row)
		}
		if value.OK() {
			points = append(points, measuredPoint{at: at, value: value, requests: row.Completed})
		}
	}
	return points
}

// measuredLatency is a series row's median latency: under load in
// every-request windows, else latency_ms; N/A when the window's latency is
// not the latency target's alone.
func measuredLatency(w snap.Window, row snap.SeriesRow) Num {
	switch {
	case !windowLatencyIsHTTP(w):
		return nan
	case !w.Metrics.EveryRequest():
		return row.LatencyMs.P50
	case row.LatencyUnderLoadMs != nil:
		return row.LatencyUnderLoadMs.P50
	}
	return nan
}

// measuredTrend compares opening/closing fifths without treating missing history as zero.
func measuredTrend(product *snap.Product, rangeID string, latency bool) Num {
	points := measuredHistory(product, rangeID, latency)
	if len(points) < 2 {
		return nan
	}
	series := make([]model.SeriesPoint, len(points))
	for i, p := range points {
		series[i] = model.SeriesPoint{V: p.value}
	}
	if latency {
		opening := mean(values(head(series, edge(len(series)))))
		if !(opening > 0) || math.IsNaN(opening) {
			return nan
		}
		return Num(jsnum.FixedNum(trendPct(series), 2))
	}
	return Num(jsnum.FixedNum(trendPts(series), 2))
}
