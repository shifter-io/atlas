package build

import (
	"math"
	"sort"
	"time"

	"github.com/Zeus-Development/proxy-index-website/api/internal/jsnum"
	"github.com/Zeus-Development/proxy-index-website/api/internal/model"
	"github.com/Zeus-Development/proxy-index-website/api/internal/snap"
)

type measuredPoint struct {
	at    time.Time
	value Num
}

// measuredHistory matches every-request.ts, using finer observed history where available.
func measuredHistory(product *snap.Product, rangeID string, latency bool) []measuredPoint {
	selected := product.Windows[rangeID]
	start, err := time.Parse(time.RFC3339Nano, selected.Period.Start)
	if err != nil {
		return nil
	}
	end, err := time.Parse(time.RFC3339Nano, selected.Period.End)
	if err != nil {
		return nil
	}
	points := []measuredPoint{}
	for _, id := range []string{"30d", "7d", "24h"} {
		w, ok := product.Windows[id]
		if !ok {
			continue
		}
		from, err := time.Parse(time.RFC3339Nano, w.Period.Start)
		if err != nil {
			continue
		}
		until, err := time.Parse(time.RFC3339Nano, w.Period.End)
		if err != nil {
			continue
		}
		if from.Before(start) || until.After(end) {
			continue
		}
		incoming := []measuredPoint{}
		for _, row := range w.Series {
			at, err := time.Parse(time.RFC3339Nano, row.At)
			if err != nil || at.Before(start) || !at.Before(end) {
				continue
			}
			value := row.SuccessRate
			if latency {
				value = nan
				if windowLatencyIsHTTP(w) {
					if w.Metrics.EveryRequest() {
						if row.LatencyUnderLoadMs != nil {
							value = row.LatencyUnderLoadMs.P50
						}
					} else {
						value = row.LatencyMs.P50
					}
				}
			}
			if value.OK() {
				incoming = append(incoming, measuredPoint{at: at, value: value})
			}
		}
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
