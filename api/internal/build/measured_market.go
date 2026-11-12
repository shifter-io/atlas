package build

import (
	"encoding/json"
	"math"
	"sort"
	"time"

	"github.com/Zeus-Development/proxy-index/api/internal/model"
)

// marketBucket sums the fleet's history points that share one age.
type marketBucket struct {
	success, weight, latency float64
	latCount                 int
}

// measuredMarket is the public All market panel.
func measuredMarket(fleet []*entry, stats map[string]map[string]*WindowStats, totals model.FleetTotals, targets []model.TargetMeta) (model.MeasuredMarket, error) {
	views := measuredViews(fleet, stats, totals)
	ips, authentic := 0.0, 0.0
	for _, e := range fleet {
		if e.P.PrimaryKind != "datacenter" {
			n := e.P.Pool.UniqueIps24h.F()
			ips += n
			authentic += n * e.P.Trust.ResidentialAuthenticity.F()
		}
	}
	targetCount := 0
	for _, t := range targets {
		if t.Source == model.SourceMeasured {
			targetCount++
		}
	}
	body, err := json.Marshal(views)
	return model.MeasuredMarket{Authenticity: Num(authentic / ips), TargetCount: Num(targetCount), Views: body}, err
}

// measuredViews is the fleet's view of each range (Ranges order), measured.
func measuredViews(fleet []*entry, stats map[string]map[string]*WindowStats, totals model.FleetTotals) []FleetView {
	views := fleetViews(fleet, stats, totals, "measured")
	for i := range views {
		measureView(&views[i], fleet, stats)
	}
	return views
}

// measureView fills one range's view from the fleet's measured figures.
func measureView(v *FleetView, fleet []*entry, stats map[string]map[string]*WindowStats) {
	buckets := measureFleet(v, fleet, stats)
	v.Fleet.Success, v.Fleet.Latency = bucketSeries(buckets)
	v.SuccessSpark, v.LatencySpark = sparkSeries(trimUnobserved(v.Fleet.Success), 24), sparkSeries(trimUnobserved(v.Fleet.Latency), 24)
	headlineCount := len(v.Fleet.Success)
	if v.Range.Days == 1 {
		headlineCount = 110
	}
	v.HeadlineSpark = sparkSeries(v.Fleet.Success, headlineCount)
	pickLeaders(v, fleet, stats)
	rankBills(&v.Bills)
}

// measureFleet sets the view's medians and bandwidth and returns the fleet's
// history points bucketed by age.
func measureFleet(v *FleetView, fleet []*entry, stats map[string]map[string]*WindowStats) map[float64]*marketBucket {
	buckets := map[float64]*marketBucket{}
	latencies, successes := []float64{}, []float64{}
	bandwidth := 0.0
	if len(fleet) == 0 {
		bandwidth = math.NaN()
	}
	for _, e := range fleet {
		s := stats[e.P.Slug][v.Range.ID]
		if s.LatencyP50.OK() {
			latencies = append(latencies, s.LatencyP50.F())
		}
		if s.SuccessRate.OK() {
			successes = append(successes, s.SuccessRate.F())
		}
		if product := e.Snap[e.P.PrimaryKind]; product != nil {
			m := product.Windows[v.Range.ID].Metrics
			bandwidth += (m.BytesUp.F() + m.BytesDown.F()) / 1e9
		} else {
			bandwidth = math.NaN()
		}
		successHistory, latencyHistory := entryHistory(e, s, v.Range.ID)
		addHistory(buckets, s, successHistory, latencyHistory)
	}
	v.Fleet.MedianLatency, v.Fleet.MedianSuccessRate = Num(median(latencies)), Num(median(successes))
	v.Fleet.BandwidthGb = Num(bandwidth)
	return buckets
}

// entryHistory is a provider's success and latency history for the range: its
// headline snapshot product's measured history, else its window stats series.
func entryHistory(e *entry, s *WindowStats, rangeID string) ([]model.SeriesPoint, []model.LatencyPoint) {
	product := e.Snap[e.P.PrimaryKind]
	if product == nil {
		return s.Success, s.Latency
	}
	end, _ := time.Parse(time.RFC3339Nano, product.Windows[rangeID].Period.End)
	successHistory, latencyHistory := []model.SeriesPoint{}, []model.LatencyPoint{}
	for _, point := range measuredHistory(product, rangeID, false) {
		successHistory = append(successHistory, model.SeriesPoint{T: Num(end.Sub(point.at).Minutes()), V: point.value})
	}
	for _, point := range measuredHistory(product, rangeID, true) {
		latencyHistory = append(latencyHistory, model.LatencyPoint{T: Num(end.Sub(point.at).Minutes()), P50: point.value})
	}
	return successHistory, latencyHistory
}

// addHistory adds one provider's history to the age buckets: success weighted
// by its requests, latency only at ages its success history opened.
func addHistory(buckets map[float64]*marketBucket, s *WindowStats, successHistory []model.SeriesPoint, latencyHistory []model.LatencyPoint) {
	for _, point := range successHistory {
		if !point.T.OK() {
			continue
		}
		b := buckets[point.T.F()]
		if b == nil {
			b = &marketBucket{}
			buckets[point.T.F()] = b
		}
		if point.V.OK() && s.Requests.OK() && s.Requests > 0 {
			b.success += point.V.F() * s.Requests.F()
			b.weight += s.Requests.F()
		}
	}
	for _, point := range latencyHistory {
		if b := buckets[point.T.F()]; b != nil && point.P50.OK() {
			b.latency += point.P50.F()
			b.latCount++
		}
	}
}

// bucketSeries is the fleet's success and latency series, oldest age first.
func bucketSeries(buckets map[float64]*marketBucket) ([]model.SeriesPoint, []model.SeriesPoint) {
	ages := make([]float64, 0, len(buckets))
	for age := range buckets {
		ages = append(ages, age)
	}
	sort.Sort(sort.Reverse(sort.Float64Slice(ages)))
	successSeries, latencySeries := []model.SeriesPoint{}, []model.SeriesPoint{}
	for _, age := range ages {
		b := buckets[age]
		success, latency := nan, nan
		if b.weight > 0 {
			success = Num(b.success / b.weight)
		}
		if b.latCount > 0 {
			latency = Num(b.latency / float64(b.latCount))
		}
		successSeries = append(successSeries, model.SeriesPoint{T: Num(age), V: success})
		latencySeries = append(latencySeries, model.SeriesPoint{T: Num(age), V: latency})
	}
	return successSeries, latencySeries
}

// pickLeaders sets the view's fastest, most reliable, cheapest and dearest
// providers; the first provider wins a tie.
func pickLeaders(v *FleetView, fleet []*entry, stats map[string]map[string]*WindowStats) {
	v.Fastest, v.MostReliable = StatPick{}, StatPick{}
	v.Cheapest, v.Dearest = CostPick{Cost: nan}, CostPick{Cost: nan}
	for _, e := range fleet {
		s := stats[e.P.Slug][v.Range.ID]
		if s.LatencyP50.OK() && (v.Fastest.Stats == nil || s.LatencyP50 < v.Fastest.Stats.LatencyP50) {
			v.Fastest = StatPick{Provider: ref(e.P), Stats: s}
		}
		if s.SuccessRate.OK() && (v.MostReliable.Stats == nil || s.SuccessRate > v.MostReliable.Stats.SuccessRate) {
			v.MostReliable = StatPick{Provider: ref(e.P), Stats: s}
		}
		price := listRate(e.P.Pricing)
		if price.OK() && (!v.Cheapest.Cost.OK() || price < v.Cheapest.Cost) {
			v.Cheapest = CostPick{Provider: ref(e.P), Cost: price}
		}
		if price.OK() && (!v.Dearest.Cost.OK() || price > v.Dearest.Cost) {
			v.Dearest = CostPick{Provider: ref(e.P), Cost: price}
		}
	}
}

// rankBills sorts the bills (missing last) and sets the best, worst and their
// spread.
func rankBills(bills *Bills) {
	sort.SliceStable(bills.All, func(i, j int) bool { return missingLast(bills.All[i].Bill, bills.All[j].Bill) < 0 })
	bills.Best, bills.Worst, bills.SpreadX = nil, nil, nan
	for i := range bills.All {
		if bills.All[i].Bill.OK() {
			b := bills.All[i]
			if bills.Best == nil {
				bills.Best = &b
			}
			bills.Worst = &b
		}
	}
	if bills.Best != nil && bills.Best.Bill > 0 {
		bills.SpreadX = bills.Worst.Bill / bills.Best.Bill
	}
}

// Fit mini charts to their metric's observed period, preserving interior gaps.
func trimUnobserved(points []model.SeriesPoint) []model.SeriesPoint {
	start, end := 0, len(points)
	for start < end && !points[start].V.OK() {
		start++
	}
	for end > start && !points[end-1].V.OK() {
		end--
	}
	return points[start:end]
}
