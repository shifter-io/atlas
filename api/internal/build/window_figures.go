package build

import (
	"math"

	"github.com/Zeus-Development/atlas/api/internal/jsnum"
	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/snap"
)

// WindowFigures are the per-range figures of a measured network beyond
// window.ts WindowStats (WINDOW-FIGURES-1), read from the snapshot window of
// the range; all null without a snapshot product.
type WindowFigures struct {
	// The window's challenge, block and request success rates are not
	// served (SUCCESS-RATE-REMOVE-1, #900): challengeScore and successScore are.
	Countries          Num `json:"countries"`
	Regions            Num `json:"regions"`
	Cities             Num `json:"cities"`
	Asns               Num `json:"asns"`
	DatacenterAsnShare Num `json:"datacenterAsnShare"`
	// DatacenterIps is DatacenterAsnShare's numerator (datacenterIps).
	DatacenterIps  Num `json:"datacenterIps"`
	ExclusiveShare Num `json:"exclusiveShare"`
	SharedShare    Num `json:"sharedShare"`
	// ExclusiveIps and ExclusiveScore are the range's /v1/overlap ones for
	// the network and kind (addExclusiveFigures); null until set.
	ExclusiveIps   Num                `json:"exclusiveIps"`
	ExclusiveScore *model.MetricScore `json:"exclusiveScore"`
	// ObservedDays is the observed span of a window shorter than its range.
	ObservedDays Num `json:"observedDays"`
	// LatencyUnderLoad tells whether the latency shown is under load: false
	// since HEADLINE-PULSE-1 (the headline is the pulses'), nil without one.
	LatencyUnderLoad *bool `json:"latencyUnderLoad"`
	// UnderLoadLatencyP50/P90/P99 are the latency under load (the daily
	// tests), display only: no score or rank reads it (HEADLINE-PULSE-1).
	UnderLoadLatencyP50 Num `json:"underLoadLatencyP50"`
	UnderLoadLatencyP90 Num `json:"underLoadLatencyP90"`
	UnderLoadLatencyP99 Num `json:"underLoadLatencyP99"`
	// CostPer1kAllTargets is the range's 8-target bill (setAllTargets).
	CostPer1kAllTargets Num `json:"costPer1kAllTargets"`
	// PartialCostPer1kAllTargets is the bill over the targets with a cost
	// when the 8-target bill is incomplete (insights.valueAll's
	// partialCostPer1k), TargetsCountedAllTargets how many it counts and
	// MissingTargetsAllTargets the targets without one ([] when complete).
	PartialCostPer1kAllTargets Num      `json:"partialCostPer1kAllTargets"`
	TargetsCountedAllTargets   Num      `json:"targetsCountedAllTargets"`
	MissingTargetsAllTargets   []string `json:"missingTargetsAllTargets"`
	// CostIsLowerBound is true when CostPer1kAllTargets counts a target
	// without successes at the highest cost any network of the kind has on it
	// (VALUE-ALL-RANK-1): the bill is at least that much; nil without a
	// snapshot product.
	CostIsLowerBound *bool `json:"costIsLowerBound"`
	// CarriedOverTargetsAllTargets is each browser card target the bill
	// prices from the last 7 days although it had no run in the range, with
	// the hours since its last run (COVERAGE-ALERT-1); {} when none, nil
	// without a snapshot product.
	CarriedOverTargetsAllTargets map[string]Num `json:"carriedOverTargetsAllTargets"`
	// SuccessScore is the range's overall success score; null when the
	// snapshot has none.
	SuccessScore *model.SuccessScore `json:"successScore"`
	// SuccessScoreDelta is SuccessScore's score minus the previous published
	// build's (SUCCESS-SCORE-DELTA-1); null when either build has none.
	SuccessScoreDelta Num `json:"successScoreDelta"`
	// ChallengeScore is the range's overall challenge score, successScore's
	// shape and rule on the targets' challenge scores (CHALLENGE-OVERALL-1:
	// 10 the fewest challenges); null when the snapshot has none.
	ChallengeScore *model.SuccessScore `json:"challengeScore"`
}

// noFigures is the figures of a network without a snapshot product.
func noFigures() WindowFigures {
	var f WindowFigures
	jsnum.FillNaN(&f)
	return f
}

// windowFigures reads a range's figures from the snapshot window w.
func windowFigures(sp *snap.Product, w snap.Window, rangeID string, ws *WindowStats) WindowFigures {
	lm, _ := latencyMetrics(w)
	under := underLoadLatency(lm)
	exclusive, shared := overlapShares(w)
	return WindowFigures{Countries: w.Pool.Countries, Regions: w.Pool.Regions, Cities: w.Pool.Cities, Asns: w.Pool.Asns,
		DatacenterAsnShare: w.Pool.DatacenterAsnShare, DatacenterIps: datacenterIps(w), ExclusiveShare: exclusive, SharedShare: shared,
		ExclusiveIps: nan, ObservedDays: observedDays(sp, rangeID), LatencyUnderLoad: latencyUnderLoad(ws),
		UnderLoadLatencyP50: under.P50, UnderLoadLatencyP90: under.P90, UnderLoadLatencyP99: under.P99,
		SuccessScore:      successScore(w),
		SuccessScoreDelta: successScoreDelta(w), ChallengeScore: challengeScore(w)}
}

// datacenterIps is the count of window w's exit IPs whose usage type is a
// hosting one: pool.datacenter_asn_share × classification_coverage ×
// unique_ips (host ÷ known × known ÷ size × size, calculated/poolfig.py),
// rounded to the integer it is, then served as a breakdown row's IPs are
// (scaledIps, K8); NaN when any of the three is null.
func datacenterIps(w snap.Window) Num {
	p := w.Pool
	if !p.DatacenterAsnShare.OK() || !p.ClassificationCoverage.OK() || !p.UniqueIps.OK() {
		return nan
	}
	return scaledIps(w, Num(math.Round(p.DatacenterAsnShare.F()*p.ClassificationCoverage.F()*p.UniqueIps.F())))
}

// overlapShares is the window's exclusive and shared share, by the provider
// rule of FIELDS.md: an observed pool compared with no peer is wholly
// exclusive (older snapshots publish null for it); no observed pool stays null.
func overlapShares(w snap.Window) (exclusive, shared Num) {
	o := w.Overlap
	if !o.ExclusiveShare.OK() && o.ComparedProducts == 0 && w.Pool.UniqueIps.OK() && w.Pool.UniqueIps > 0 {
		return 1, 0
	}
	return o.ExclusiveShare, o.SharedShare
}

// observedDays ports the old site's observed period (MarketPanel
// observedPhrase for one network): the days from the range's first history
// point to the window's end, rounded up; 0 below one day; NaN when the
// history covers the range or there is none.
func observedDays(sp *snap.Product, rangeID string) Num {
	points := measuredHistory(sp, rangeID, false)
	_, end, ok := measuredPeriod(sp.Windows[rangeID])
	if len(points) == 0 || !ok {
		return nan
	}
	days := end.Sub(points[0].at).Hours() / 24
	switch {
	case days >= float64(rangeMeta(rangeID).Days):
		return nan
	case days < 1:
		return 0
	}
	return Num(math.Ceil(days))
}

// latencyUnderLoad tells whether the headline latency is under load: false
// whenever there is one, as the headline is the pulses' latency_ms
// (HEADLINE-PULSE-1); nil without latency.
func latencyUnderLoad(ws *WindowStats) *bool {
	if !ws.LatencyP50.OK() {
		return nil
	}
	under := false
	return &under
}

// wireBytes is the window's wire traffic, both directions (bytes_up +
// bytes_down), as the market's bandwidthGb sums it; NaN when the window
// recorded no bytes. Bandwidth is in decimal units (GB = 10⁹ bytes), as the
// cost per 1,000 successes.
func wireBytes(m snap.Metrics) Num {
	bytes := m.BytesUp + m.BytesDown
	if !bytes.OK() || bytes <= 0 {
		return nan
	}
	return bytes
}

// bandwidthMb is the window's wire traffic in MB (10⁶ bytes).
func bandwidthMb(m snap.Metrics) Num { return wireBytes(m) / 1e6 }

// wireGbPer1k is the GB (10^9 bytes, gbBytes) of wire traffic (up + down) per 1,000
// completed requests; NaN without wire bytes or requests.
func wireGbPer1k(m snap.Metrics) Num {
	bytes := m.BytesUp + m.BytesDown
	if !bytes.OK() || !m.Completed.OK() || m.Completed <= 0 {
		return nan
	}
	return Num(bytes.F() / m.Completed.F() * 1000 / gbBytes)
}

// wireKbPerRequest is the KB (1024 bytes) of wire traffic per completed
// request, rounded to 2 dp: the wire bytes wireGbPer1k reads (pricing.avgPayloadKb).
// NaN without wire bytes or requests.
func wireKbPerRequest(m snap.Metrics) Num {
	gb := wireGbPer1k(m)
	if !gb.OK() || gb <= 0 {
		return nan
	}
	return Num(math.Round(gb.F()*gbBytes/1000/1024*100) / 100)
}

// underLoadSeries is the window's latency under load per series row
// (series[].latency_under_load_ms), null where a row has none or the window's
// latency is not the latency target's alone.
func underLoadSeries(w snap.Window, age func(string) Num) []model.LatencyPoint {
	http := windowLatencyIsHTTP(w)
	out := make([]model.LatencyPoint, 0, len(w.Series))
	for _, s := range w.Series {
		l := naLatency
		if http && s.LatencyUnderLoadMs != nil {
			l = *s.LatencyUnderLoadMs
		}
		out = append(out, model.LatencyPoint{T: age(s.At), P50: l.P50, P90: l.P90, P99: l.P99})
	}
	return out
}
