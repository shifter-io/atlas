package build

import (
	"encoding/json"
	"math"
	"sort"
	"time"

	"github.com/ice-zeus/milf-website/api/internal/model"
)

// measuredMarket is the public All market panel. Demo entries never enter it.
func measuredMarket(fleet []*entry, stats map[string]map[string]*WindowStats, totals model.FleetTotals, targets []model.TargetMeta) (model.MeasuredMarket, error) {
	views := fleetViews(fleet, stats, totals, "measured")
	for i := range views {
		v := &views[i]
		type bucket struct {
			success, weight, latency float64
			latCount                 int
		}
		buckets := map[float64]*bucket{}
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
				bandwidth += (m.BytesUp.F() + m.BytesDown.F()) / (1024 * 1024 * 1024)
			} else {
				bandwidth = math.NaN()
			}
			successHistory, latencyHistory := s.Success, s.Latency
			if product := e.Snap[e.P.PrimaryKind]; product != nil {
				end, _ := time.Parse(time.RFC3339Nano, product.Windows[v.Range.ID].Period.End)
				successHistory, latencyHistory = []model.SeriesPoint{}, []model.LatencyPoint{}
				for _, point := range measuredHistory(product, v.Range.ID, false) {
					successHistory = append(successHistory, model.SeriesPoint{T: Num(end.Sub(point.at).Minutes()), V: point.value})
				}
				for _, point := range measuredHistory(product, v.Range.ID, true) {
					latencyHistory = append(latencyHistory, model.LatencyPoint{T: Num(end.Sub(point.at).Minutes()), P50: point.value})
				}
			}
			for _, point := range successHistory {
				if !point.T.OK() {
					continue
				}
				b := buckets[point.T.F()]
				if b == nil {
					b = &bucket{}
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
		v.Fleet.MedianLatency, v.Fleet.MedianSuccessRate = Num(median(latencies)), Num(median(successes))
		v.Fleet.BandwidthGb = Num(bandwidth)
		ages := make([]float64, 0, len(buckets))
		for age := range buckets {
			ages = append(ages, age)
		}
		sort.Sort(sort.Reverse(sort.Float64Slice(ages)))
		v.Fleet.Success, v.Fleet.Latency = []model.SeriesPoint{}, []model.SeriesPoint{}
		for _, age := range ages {
			b := buckets[age]
			success, latency := nan, nan
			if b.weight > 0 {
				success = Num(b.success / b.weight)
			}
			if b.latCount > 0 {
				latency = Num(b.latency / float64(b.latCount))
			}
			v.Fleet.Success = append(v.Fleet.Success, model.SeriesPoint{T: Num(age), V: success})
			v.Fleet.Latency = append(v.Fleet.Latency, model.SeriesPoint{T: Num(age), V: latency})
		}
		v.SuccessSpark, v.LatencySpark = sparkSeries(trimUnobserved(v.Fleet.Success), 24), sparkSeries(trimUnobserved(v.Fleet.Latency), 24)
		headlineCount := len(v.Fleet.Success)
		if v.Range.Days == 1 {
			headlineCount = 110
		}
		v.HeadlineSpark = sparkSeries(v.Fleet.Success, headlineCount)
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
		sort.SliceStable(v.Bills.All, func(i, j int) bool { return missingLast(v.Bills.All[i].Bill, v.Bills.All[j].Bill) < 0 })
		v.Bills.Best, v.Bills.Worst, v.Bills.SpreadX = nil, nil, nan
		for i := range v.Bills.All {
			if v.Bills.All[i].Bill.OK() {
				b := v.Bills.All[i]
				if v.Bills.Best == nil {
					v.Bills.Best = &b
				}
				v.Bills.Worst = &b
			}
		}
		if v.Bills.Best != nil && v.Bills.Best.Bill > 0 {
			v.Bills.SpreadX = v.Bills.Worst.Bill / v.Bills.Best.Bill
		}
	}
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
