package build

import (
	"math"
	"sort"
	"strconv"

	"github.com/Zeus-Development/proxy-index/api/internal/jsnum"
	"github.com/Zeus-Development/proxy-index/api/internal/model"
)

// bucketLabel is format.ts bucketLabel.
func bucketLabel(minutesAgo float64) string {
	m := math.Max(0, jsnum.Round(minutesAgo))
	if math.IsNaN(minutesAgo) {
		m = math.NaN()
	}
	switch {
	case m == 0:
		return "now"
	case m < 60:
		return jsnum.ToFixed(m, 0) + " min ago"
	case m < 1440:
		hours := math.Floor(m / 60)
		rest := math.Mod(m, 60)
		if rest != 0 {
			return jsnum.ToFixed(hours, 0) + "h " + jsnum.ToFixed(rest, 0) + "m ago"
		}
		return jsnum.ToFixed(hours, 0) + "h ago"
	}
	days := jsnum.Round(m / 1440)
	if days == 1 {
		return "1 day ago"
	}
	if math.IsNaN(days) {
		return "NaN days ago"
	}
	return strconv.FormatFloat(days, 'f', -1, 64) + " days ago"
}

// downsample is data.ts downsample.
func downsample[T any](items []T, count int) []T {
	if len(items) <= count {
		return items
	}
	step := float64(len(items)-1) / float64(count-1)
	out := make([]T, count)
	for i := range out {
		out[i] = items[int(jsnum.Round(float64(i)*step))]
	}
	return out
}

func sparkSeries(points []model.SeriesPoint, count int) SparkSeries {
	thinned := downsample(points, count)
	s := SparkSeries{Values: make([]Num, len(thinned)), Labels: make([]string, len(thinned))}
	for i, p := range thinned {
		s.Values[i] = p.V
		s.Labels[i] = bucketLabel(p.T.F())
	}
	return s
}

func ref(p *model.Provider) *ProviderRef {
	return &ProviderRef{Slug: p.Slug, Name: p.Name, Source: p.Source}
}

// providerStats is one network with its stats over one range.
type providerStats struct {
	p *model.Provider
	s *WindowStats
}

// fleetViews ports overview.ts fleetViews.
func fleetViews(fleet []*entry, stats map[string]map[string]*WindowStats, totals model.FleetTotals, basis string) []FleetView {
	out := []FleetView{}
	for _, r := range Ranges {
		out = append(out, fleetView(fleet, stats, totals, r, basis))
	}
	return out
}

// fleetView is the fleet view of one range.
func fleetView(fleet []*entry, stats map[string]map[string]*WindowStats, totals model.FleetTotals, r RangeMeta, basis string) FleetView {
	fw := fleetWindow(fleet, stats, totals, r.ID, basis)
	all := make([]providerStats, 0, len(fleet))
	for _, e := range fleet {
		all = append(all, providerStats{e.P, stats[e.P.Slug][r.ID]})
	}
	headlineCount := len(fw.Success)
	if r.Days == 1 {
		headlineCount = 110
	}
	latencyVals := make([]model.SeriesPoint, len(fw.Latency))
	copy(latencyVals, fw.Latency)
	v := FleetView{Range: r, Fleet: fw,
		LatencySpark: sparkSeries(latencyVals, 24), SuccessSpark: sparkSeries(fw.Success, 24),
		HeadlineSpark: sparkSeries(fw.Success, headlineCount),
		Cheapest:      CostPick{Cost: nan}, Dearest: CostPick{Cost: nan}}
	if len(all) > 0 {
		setPicks(&v, all)
	}
	return v
}

// setPicks sets v's fastest, most reliable, cheapest and dearest networks of
// all, which is not empty.
func setPicks(v *FleetView, all []providerStats) {
	fast := append([]providerStats{}, all...)
	sort.SliceStable(fast, func(i, j int) bool { return fast[i].s.LatencyP50-fast[j].s.LatencyP50 < 0 })
	rel := append([]providerStats{}, all...)
	sort.SliceStable(rel, func(i, j int) bool { return rel[j].s.SuccessRate-rel[i].s.SuccessRate < 0 })
	byGb := append([]providerStats{}, all...)
	// By the `$ / GB` shown (the lowest list rate), unpriced networks last.
	sort.SliceStable(byGb, func(i, j int) bool {
		return missingLast(displayRate(byGb[i].p.Pricing), displayRate(byGb[j].p.Pricing)) < 0
	})
	v.Fastest = StatPick{Provider: ref(fast[0].p), Stats: fast[0].s}
	v.MostReliable = StatPick{Provider: ref(rel[0].p), Stats: rel[0].s}
	v.Cheapest = CostPick{Provider: ref(byGb[0].p), Cost: displayRate(byGb[0].p.Pricing)}
	last := byGb[len(byGb)-1]
	v.Dearest = CostPick{Provider: ref(last.p), Cost: displayRate(last.p.Pricing)}
}

// missingLast is format.ts missingLast (ascending): missing values after every
// real one.
func missingLast(a, b Num) float64 {
	am, bm := !a.OK(), !b.OK()
	if am || bm {
		switch {
		case am == bm:
			return 0
		case am:
			return 1
		}
		return -1
	}
	return a.F() - b.F()
}
