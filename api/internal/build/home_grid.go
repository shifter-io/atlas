package build

import (
	"cmp"
	"slices"

	"github.com/Zeus-Development/proxy-index/api/internal/model"
)

// The home's network curves share one time axis per range (SPARK-TIMES-1):
// each network's curve has a value per grid time, null where the network
// has no point, so the answer holds one labels and times list for every
// curve instead of one per network.

// homeHistory is a ranking row's success and latency (p50) histories for the
// range; empty for a row without an entry or window stats.
type homeHistory struct {
	success, latency []model.SeriesPoint
}

// homeHistories is each row's homeHistory, in the rows' order.
func homeHistories(rows []RankingRow, bySlug map[string]*entry, stats map[string]map[string]*WindowStats, rangeID string) []homeHistory {
	out := make([]homeHistory, len(rows))
	for i, row := range rows {
		e, s := bySlug[row.Slug], stats[row.Slug][rangeID]
		if e == nil || s == nil {
			continue
		}
		success, latency := entryHistory(e, s, rangeID)
		out[i].success = success
		for _, p := range latency {
			out[i].latency = append(out[i].latency, model.SeriesPoint{T: p.T, V: p.P50})
		}
	}
	return out
}

// curveGrid is the range's curve axis: every time of the rows' success
// histories, oldest first; for a one-day range thinned to homeCurve24h
// points (downsample), the old site's 24h curve count.
func curveGrid(hists []homeHistory, r RangeMeta) []Num {
	seen, grid := map[Num]bool{}, []Num{}
	for _, h := range hists {
		for _, p := range h.success {
			if p.T.OK() && !seen[p.T] {
				seen[p.T], grid = true, append(grid, p.T)
			}
		}
	}
	slices.SortFunc(grid, func(a, b Num) int { return cmp.Compare(b, a) })
	if r.Days == 1 {
		return downsample(grid, homeCurve24h)
	}
	return grid
}

// gridSeries is points on grid: each grid time's value, null where points
// has none; empty without points (a row with no history has no curve).
func gridSeries(points []model.SeriesPoint, grid []Num) SparkSeries {
	if len(points) == 0 {
		return SparkSeries{Values: []Num{}, Labels: []string{}, Times: []Num{}}
	}
	at := make(map[Num]Num, len(points))
	for _, p := range points {
		at[p.T] = p.V
	}
	s := SparkSeries{Values: make([]Num, len(grid)), Labels: make([]string, len(grid)), Times: grid}
	for i, t := range grid {
		v, ok := at[t]
		if !ok {
			v = nan
		}
		s.Values[i], s.Labels[i] = v, bucketLabel(t.F())
	}
	return s
}
