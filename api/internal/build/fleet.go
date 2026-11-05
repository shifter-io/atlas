package build

import (
	"math"
	"sort"
	"strconv"

	"github.com/Zeus-Development/proxy-index-website/api/internal/jsnum"
	"github.com/Zeus-Development/proxy-index-website/api/internal/model"
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

// fleetViews ports overview.ts fleetViews.
func fleetViews(fleet []*entry, stats map[string]map[string]*WindowStats, totals model.FleetTotals, basis string) []FleetView {
	out := []FleetView{}
	for _, r := range Ranges {
		fw := fleetWindow(fleet, stats, totals, r.ID, basis)
		type ps struct {
			p *model.Provider
			s *WindowStats
		}
		all := make([]ps, 0, len(fleet))
		for _, e := range fleet {
			all = append(all, ps{e.P, stats[e.P.Slug][r.ID]})
		}
		byCost := append([]ps{}, all...)
		sort.SliceStable(byCost, func(i, j int) bool { return byCost[i].s.CostPer1kSuccess-byCost[j].s.CostPer1kSuccess < 0 })
		byGb := append([]ps{}, all...)
		// By the `$ / GB` shown (the lowest list rate), unpriced networks last.
		sort.SliceStable(byGb, func(i, j int) bool { return missingLast(listRate(byGb[i].p.Pricing), listRate(byGb[j].p.Pricing)) < 0 })
		bills := Bills{All: []Bill{}, SpreadX: nan}
		for _, x := range byCost {
			if x.p.PrimaryKind == "residential" {
				bills.All = append(bills.All, Bill{Provider: *ref(x.p), Bill: x.s.CostPer1kSuccess * 1000})
			}
		}
		if len(bills.All) > 0 {
			best, worst := bills.All[0], bills.All[len(bills.All)-1]
			bills.Best, bills.Worst = &best, &worst
			bills.SpreadX = worst.Bill / best.Bill
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
			Cheapest:      CostPick{Cost: nan}, Dearest: CostPick{Cost: nan}, Bills: bills}
		if len(all) > 0 {
			fast := append([]ps{}, all...)
			sort.SliceStable(fast, func(i, j int) bool { return fast[i].s.LatencyP50-fast[j].s.LatencyP50 < 0 })
			rel := append([]ps{}, all...)
			sort.SliceStable(rel, func(i, j int) bool { return rel[j].s.SuccessRate-rel[i].s.SuccessRate < 0 })
			v.Fastest = StatPick{Provider: ref(fast[0].p), Stats: fast[0].s}
			v.MostReliable = StatPick{Provider: ref(rel[0].p), Stats: rel[0].s}
			v.Cheapest = CostPick{Provider: ref(byGb[0].p), Cost: listRate(byGb[0].p.Pricing)}
			last := byGb[len(byGb)-1]
			v.Dearest = CostPick{Provider: ref(last.p), Cost: listRate(last.p.Pricing)}
		}
		out = append(out, v)
	}
	return out
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
