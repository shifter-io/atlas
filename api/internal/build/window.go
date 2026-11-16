package build

import (
	"math"
	"sort"

	"github.com/Zeus-Development/atlas/api/internal/jsnum"
	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/snap"
)

const (
	daySeconds = 86400.0
	dayMinutes = 1440.0
)

func mean(values []float64) float64 {
	if len(values) == 0 {
		return 0
	}
	s := 0.0
	for _, v := range values {
		s += v
	}
	return s / float64(len(values))
}

// median is window.ts median: sorted copy, mid element or mean of the two.
func median(values []float64) float64 {
	sorted := append([]float64{}, values...)
	jsSortNumbers(sorted)
	if len(sorted) == 0 {
		return math.NaN()
	}
	mid := len(sorted) / 2
	if len(sorted)%2 == 1 {
		return sorted[mid]
	}
	return (sorted[mid-1] + sorted[mid]) / 2
}

// jsSortNumbers sorts like [].sort((a, b) => a - b): a NaN comparison is
// "equal", so NaN entries stay put relative to their neighbours as far as a
// stable sort allows.
func jsSortNumbers(v []float64) {
	sort.SliceStable(v, func(i, j int) bool { return v[i]-v[j] < 0 })
}

func tail[T any](points []T, days int) []T {
	if days >= len(points) {
		return points
	}
	if days <= 0 {
		// JS slice(-0) is the whole array.
		return points
	}
	return points[len(points)-days:]
}

func values(points []model.SeriesPoint) []float64 {
	out := make([]float64, len(points))
	for i, p := range points {
		out[i] = p.V.F()
	}
	return out
}

func edge(n int) int {
	e := int(jsnum.Round(float64(n) / 5))
	if e < 1 {
		e = 1
	}
	return e
}

func head[T any](points []T, n int) []T {
	if n > len(points) {
		return points
	}
	return points[:n]
}

func trendPts(points []model.SeriesPoint) float64 {
	e := edge(len(points))
	return (mean(values(tail(points, e))) - mean(values(head(points, e)))) * 100
}

func trendPct(points []model.SeriesPoint) float64 {
	e := edge(len(points))
	opening := mean(values(head(points, e)))
	if opening == 0 || math.IsNaN(opening) {
		return 0
	}
	return ((mean(values(tail(points, e))) - opening) / opening) * 100
}

type poolCurve struct {
	exponent float64
	day      float64
}

func (c poolCurve) at(days float64) float64 {
	return jsnum.Round(c.day * math.Pow(days, c.exponent))
}

func curveOf(p *model.Provider) poolCurve {
	day, month := p.Pool.UniqueIps24h.F(), p.Pool.UniqueIps30d.F()
	exp := 0.0
	if day > 0 && month > day {
		exp = math.Log(month/day) / math.Log(30)
	}
	return poolCurve{exponent: exp, day: day}
}

func recurringShareAt(published30 float64, days int) Num {
	if days <= 1 {
		return nan
	}
	if days >= 30 {
		return Num(published30)
	}
	ratio := math.Log(float64(days)) / math.Log(30)
	return Num(jsnum.FixedNum(published30*math.Pow(ratio, 1-published30), 4))
}

func incidentOverlapMinutes(inc model.Incident, windowSeconds float64) float64 {
	ended := inc.StartedAgoSeconds.F() - float64(inc.DurationMinutes.F()*60)
	overlap := math.Min(inc.StartedAgoSeconds.F(), windowSeconds) - math.Max(ended, 0)
	return math.Max(0, overlap/60)
}

type availability struct {
	uptimePct, downtimeMinutes float64
	incidentCount              int
	bands                      []IncidentBand
}

func availabilityAt(incidents []model.Incident, days int) availability {
	windowSeconds := float64(days) * daySeconds
	downtime := 0.0
	bands := []IncidentBand{}
	for _, inc := range incidents {
		overlap := incidentOverlapMinutes(inc, windowSeconds)
		if !(overlap > 0) {
			continue
		}
		downtime += float64(overlap * inc.Impact.F()) // explicit conversion: no fused multiply-add, like JS
		ended := inc.StartedAgoSeconds.F() - float64(inc.DurationMinutes.F()*60)
		from := 1 - math.Min(inc.StartedAgoSeconds.F(), windowSeconds)/windowSeconds
		to := 1 - math.Max(ended, 0)/windowSeconds
		if to > from {
			bands = append(bands, IncidentBand{From: Num(from), To: Num(to), Severity: inc.Severity, Country: inc.Scope})
		}
	}
	up := math.Max(0, math.Min(100, 100-float64((downtime/(float64(days)*dayMinutes))*100)))
	return availability{uptimePct: jsnum.FixedNum(up, 3), downtimeMinutes: jsnum.FixedNum(downtime, 1), incidentCount: len(bands), bands: bands}
}

// windowStats ports window.ts windowStats. sp is the snapshot product behind
// a measured provider's headline (nil without a snapshot product).
func windowStats(p *model.Provider, sp *snap.Product, rangeID string, cards []string) *WindowStats {
	meta := rangeMeta(rangeID)
	if sp != nil {
		ws := measuredWindowStats(p, sp, rangeID, meta)
		value := rangeValue(sp, rangeID)
		ws.setAllTargets(costRate(p.Pricing), pricedWindow(sp, rangeID, value), cards, value)
		return ws
	}
	av := availabilityAt(p.Incidents, meta.Days)
	if rangeID == "24h" {
		return published24hStats(p, meta, av)
	}
	return publishedWindowStats(p, meta, av)
}

// measuredWindowStats is a range's stats from the provider's snapshot product.
func measuredWindowStats(p *model.Provider, sp *snap.Product, rangeID string, meta RangeMeta) *WindowStats {
	w := sp.Window(rangeID)
	m := w.Metrics
	end := snap.Ms(w.Period.End)
	age := func(at string) Num { return Num((end - snap.Ms(at)) / 60000) }
	uptime := nan
	if m.Uptime.OK() {
		uptime = m.Uptime * 100
	}
	lm, target := latencyMetrics(w)
	head := headlineLatency(lm)
	successDelta, successEdges := measuredTrend(sp, rangeID, false)
	latencyDelta, latencyEdges, latencyWhy := latencyChange(sp, rangeID)
	ws := &WindowStats{Range: meta, SuccessRate: m.SuccessRate, RequestSuccessRate: requestRate(m), SuccessDeltaPts: successDelta, successEdges: successEdges, latencyEdges: latencyEdges, latencyWhy: latencyWhy,
		LatencyP50: head.P50, LatencyP90: head.P90, LatencyP99: head.P99,
		LightLatencyP50: head.P50, LightLatencyP90: head.P90, LightLatencyP99: head.P99, LatencyTarget: strOrNil(target), LatencyDeltaPct: latencyDelta,
		UniqueIps: ipsOf(w), ChurnRate: w.Pool.ChurnRate, RecurringShare: w.Pool.RecurringShare,
		Requests: m.Completed, BandwidthMb: bandwidthMb(m), CostPer1kSuccess: wireCostPer1k(costRate(p.Pricing), m), UptimePct: uptime, DowntimeMinutes: weightedDowntimeMinutes(w.Uptime),
		IncidentCount: nan, Bands: []IncidentBand{}, Success: []model.SeriesPoint{}, Availability: []model.SeriesPoint{}, Latency: []model.LatencyPoint{}, Pool: []model.SeriesPoint{}}
	ws.WindowFigures = windowFigures(sp, w, rangeID, ws)
	// Outages (snapshot v2) shade the charts and count as incidents; uptime
	// and downtime stay the framework's own time-based figures.
	if sp.Outages != nil {
		av := availabilityAt(p.Incidents, meta.Days)
		ws.Bands, ws.IncidentCount = av.bands, Num(av.incidentCount)
	}
	for _, s := range w.Series {
		t := age(s.At)
		ws.Success = append(ws.Success, model.SeriesPoint{T: t, V: s.SuccessRate})
		ws.Availability = append(ws.Availability, model.SeriesPoint{T: t, V: s.Uptime})
		l := seriesLatency(w, s)
		ws.Latency = append(ws.Latency, model.LatencyPoint{T: t, P50: l.P50, P90: l.P90, P99: l.P99})
	}
	ws.Pool = ipPoints(w, rangeID, w.Series, age)
	ws.UnderLoad = underLoadSeries(w, age)
	return ws
}

// published24hStats is the 24h stats of a provider without a snapshot
// product, from its published figures.
func published24hStats(p *model.Provider, meta RangeMeta, av availability) *WindowStats {
	return &WindowStats{Range: meta, SuccessRate: p.Performance.SuccessRate, RequestSuccessRate: p.Performance.SuccessRate,
		SuccessDeltaPts: p.Performance.SuccessRateDelta24h,
		LatencyP50:      p.Performance.LatencyP50, LatencyP90: p.Performance.LatencyP90, LatencyP99: p.Performance.LatencyP99,
		LightLatencyP50: nan, LightLatencyP90: nan, LightLatencyP99: nan,
		LatencyDeltaPct: p.Performance.LatencyDelta24h, UniqueIps: p.Pool.UniqueIps24h, ChurnRate: p.Pool.ChurnRate,
		RecurringShare: nan, Requests: p.Performance.Requests24h, BandwidthMb: p.Performance.BandwidthMb24h,
		CostPer1kSuccess: p.Pricing.CostPer1kSuccess, UptimePct: Num(av.uptimePct), DowntimeMinutes: Num(av.downtimeMinutes),
		IncidentCount: Num(av.incidentCount), WindowFigures: noFigures(), Bands: av.bands, Success: nonNilSeries(p.Series.Success24h),
		Availability: incidentAvailabilitySeries(p.Incidents, meta.Days), Latency: nonNilLatency(p.Series.Latency24h), Pool: nonNilSeries(p.Series.UniqueIps24h),
		UnderLoad: []model.LatencyPoint{}}
}

// publishedWindowStats is a longer range's stats of a provider without a
// snapshot product: its daily series tails and its published figures scaled
// to the range.
func publishedWindowStats(p *model.Provider, meta RangeMeta, av availability) *WindowStats {
	days := meta.Days
	success := tail(p.Series.Success30d, days)
	latencyDaily := tail(p.Series.Latency30d, days)
	pool := tail(p.Series.UniqueIps30d, days)
	p50 := mean(values(latencyDaily))
	spread90 := p.Performance.LatencyP90.F() / p.Performance.LatencyP50.F()
	spread99 := p.Performance.LatencyP99.F() / p.Performance.LatencyP50.F()
	curve := curveOf(p)
	uniqueIps := p.Pool.UniqueIps30d.F()
	if days != 30 {
		uniqueIps = curve.at(float64(days))
	}
	successRate := mean(values(success))
	delta := p.Performance.SuccessRateDelta30d.F()
	if days != 30 {
		delta = jsnum.FixedNum(trendPts(success), 2)
	}
	cost := p.Pricing.CostPer1kSuccess.F()
	if successRate != 0 && !math.IsNaN(successRate) {
		cost = (p.Pricing.CostPer1kSuccess.F() * p.Performance.SuccessRate.F()) / successRate
	}
	lat := make([]model.LatencyPoint, 0, len(latencyDaily))
	for _, pt := range latencyDaily {
		lat = append(lat, model.LatencyPoint{T: pt.T, P50: pt.V, P90: Num(jsnum.Round(pt.V.F() * spread90)), P99: Num(jsnum.Round(pt.V.F() * spread99))})
	}
	return &WindowStats{Range: meta, SuccessRate: Num(successRate), RequestSuccessRate: Num(successRate), SuccessDeltaPts: Num(delta),
		LatencyP50: Num(jsnum.Round(p50)), LatencyP90: Num(jsnum.Round(p50 * spread90)), LatencyP99: Num(jsnum.Round(p50 * spread99)),
		LightLatencyP50: nan, LightLatencyP90: nan, LightLatencyP99: nan,
		LatencyDeltaPct: Num(jsnum.FixedNum(trendPct(latencyDaily), 2)), UniqueIps: Num(uniqueIps),
		ChurnRate:      Num(math.Min(1, p.Pool.ChurnRate.F()*math.Pow(float64(days), curve.exponent-1))),
		RecurringShare: recurringShareAt(p.Pool.RecurringShare.F(), days),
		Requests:       p.Performance.Requests24h * Num(days), BandwidthMb: Num(jsnum.FixedNum(p.Performance.BandwidthMb24h.F()*float64(days), 1)),
		CostPer1kSuccess: Num(cost), UptimePct: Num(av.uptimePct), DowntimeMinutes: Num(av.downtimeMinutes),
		IncidentCount: Num(av.incidentCount), WindowFigures: noFigures(), Bands: av.bands, Success: nonNilSeries(success),
		Availability: incidentAvailabilitySeries(p.Incidents, meta.Days), Latency: lat, Pool: nonNilSeries(pool), UnderLoad: []model.LatencyPoint{}}
}

func incidentAvailabilitySeries(incidents []model.Incident, days int) []model.SeriesPoint {
	step := dayMinutes
	if days == 1 {
		step = 5
	}
	points := []model.SeriesPoint{}
	for older := float64(days) * dayMinutes; older > 0; older -= step {
		younger := math.Max(0, older-step)
		down := 0.0
		for _, inc := range incidents {
			start := inc.StartedAgoSeconds.F() / 60
			end := start - inc.DurationMinutes.F()
			down += float64(math.Max(0, math.Min(start, older)-math.Max(end, younger)) * inc.Impact.F())
		}
		v := math.Max(0, math.Min(1, 1-down/(older-younger)))
		points = append(points, model.SeriesPoint{T: Num(older), V: Num(jsnum.FixedNum(v, 6))})
	}
	return points
}

func nonNilSeries(s []model.SeriesPoint) []model.SeriesPoint {
	if s == nil {
		return []model.SeriesPoint{}
	}
	return s
}

func nonNilLatency(s []model.LatencyPoint) []model.LatencyPoint {
	if s == nil {
		return []model.LatencyPoint{}
	}
	return s
}

// fleetWindow ports window.ts fleetWindow over the measured providers.
func fleetWindow(fleet []*entry, stats map[string]map[string]*WindowStats, totals model.FleetTotals, rangeID, basis string) FleetWindow {
	meta := rangeMeta(rangeID)
	fw := FleetWindow{Range: meta, Requests: 0, UniqueIps: 0, MedianLatency: nan, MedianSuccessRate: nan, BandwidthGb: nan,
		Success: []model.SeriesPoint{}, Latency: []model.SeriesPoint{}, Basis: basis}
	ws := make([]*WindowStats, 0, len(fleet))
	for _, e := range fleet {
		ws = append(ws, stats[e.P.Slug][rangeID])
	}
	total := 0.0
	unique := 0.0
	for _, s := range ws {
		total += s.Requests.F()
		unique += s.UniqueIps.F()
	}
	fw.Requests, fw.UniqueIps = Num(total), Num(unique)
	if len(ws) > 0 {
		schedule := ws[0].Success
		for i, bucket := range schedule {
			sum := 0.0
			lat := make([]float64, 0, len(ws))
			for _, s := range ws {
				v := 0.0
				if i < len(s.Success) {
					v = s.Success[i].V.F()
				}
				sum += float64(v * s.Requests.F())
				l := 0.0
				if i < len(s.Latency) {
					l = s.Latency[i].P50.F()
				}
				lat = append(lat, l)
			}
			fw.Success = append(fw.Success, model.SeriesPoint{T: bucket.T, V: Num(sum / total)})
			fw.Latency = append(fw.Latency, model.SeriesPoint{T: bucket.T, V: Num(mean(lat))})
		}
	}
	if rangeID == "24h" {
		fw.MedianLatency, fw.MedianSuccessRate = totals.MedianLatency, totals.MedianSuccessRate
	} else if len(ws) > 0 {
		lat := make([]float64, len(ws))
		sr := make([]float64, len(ws))
		for i, s := range ws {
			lat[i], sr[i] = s.LatencyP50.F(), s.SuccessRate.F()
		}
		fw.MedianLatency = Num(jsnum.Round(median(lat)))
		fw.MedianSuccessRate = Num(median(sr))
	}
	fw.BandwidthGb = Num(jsnum.Round(totals.BandwidthGb24h.F() * float64(meta.Days)))
	return fw
}

func strOrNil(s string) *string {
	if s == "" {
		return nil
	}
	return &s
}
