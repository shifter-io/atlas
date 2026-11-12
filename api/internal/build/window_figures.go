package build

import (
	"math"
	"strings"
	"unicode"

	"github.com/Zeus-Development/proxy-index/api/internal/jsnum"
	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/Zeus-Development/proxy-index/api/internal/snap"
)

// WindowFigures are the per-range figures of a measured network beyond
// window.ts WindowStats (WINDOW-FIGURES-1), read from the snapshot window of
// the range; all null without a snapshot product.
type WindowFigures struct {
	// CaptchaRate and BlockRate are metrics.challenge_rate and block_rate.
	CaptchaRate Num `json:"captchaRate"`
	BlockRate   Num `json:"blockRate"`
	// RequestSuccessRate is metrics.request_success_rate: successes ÷
	// requests, beside the time-weighted SuccessRate.
	RequestSuccessRate Num `json:"requestSuccessRate"`
	Countries          Num `json:"countries"`
	Regions            Num `json:"regions"`
	Cities             Num `json:"cities"`
	Asns               Num `json:"asns"`
	DatacenterAsnShare Num `json:"datacenterAsnShare"`
	ExclusiveShare     Num `json:"exclusiveShare"`
	SharedShare        Num `json:"sharedShare"`
	// ObservedDays is the observed span of a window shorter than its range.
	ObservedDays Num `json:"observedDays"`
	// LatencyUnderLoad tells whether the latency shown is under load.
	LatencyUnderLoad *bool   `json:"latencyUnderLoad"`
	LatencyNote      *string `json:"latencyNote"`
	WireGbPer1k      Num     `json:"wireGbPer1k"`
	// SuccessScore is the range's overall success score; null when the
	// snapshot has none.
	SuccessScore *model.SuccessScore `json:"successScore"`
}

// noFigures is the figures of a network without a snapshot product.
func noFigures() WindowFigures {
	var f WindowFigures
	jsnum.FillNaN(&f)
	return f
}

// windowFigures reads a range's figures from the snapshot window w.
func windowFigures(sp *snap.Product, w snap.Window, rangeID string, ws *WindowStats) WindowFigures {
	m := w.Metrics
	lm, _ := latencyMetrics(w)
	exclusive, shared := overlapShares(w)
	return WindowFigures{CaptchaRate: m.ChallengeRate, BlockRate: m.BlockRate, RequestSuccessRate: m.RequestSuccessRate,
		Countries: w.Pool.Countries, Regions: w.Pool.Regions, Cities: w.Pool.Cities, Asns: w.Pool.Asns,
		DatacenterAsnShare: w.Pool.DatacenterAsnShare, ExclusiveShare: exclusive, SharedShare: shared,
		ObservedDays: observedDays(sp, rangeID), LatencyUnderLoad: latencyUnderLoad(lm, ws),
		LatencyNote: latencyNote(ws), WireGbPer1k: wireGbPer1k(m), SuccessScore: successScore(w)}
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

// latencyUnderLoad is true when the headline latency is under load (lm, the
// latency target's metrics, from a load test), false when the latency shown
// is light-load (older snapshots, or an every-request window without a load
// test) and nil without latency.
func latencyUnderLoad(lm snap.Metrics, ws *WindowStats) *bool {
	under := ws.LatencyP50.OK() && underLoad(lm)
	if !ws.LatencyP50.OK() && !ws.LightLatencyP50.OK() {
		return nil
	}
	return &under
}

// latencyNote ports every-request.ts latencyNote: the latency target's label,
// with ", under load" when the headline is (a light-load latency beside it);
// nil without a target.
func latencyNote(ws *WindowStats) *string {
	if ws.LatencyTarget == nil {
		return nil
	}
	note := targetLabel(*ws.LatencyTarget)
	if ws.LightLatencyP50.OK() {
		note += ", under load"
	}
	return &note
}

// targetLabel ports every-request.ts targetLabel: a readable target name.
func targetLabel(id string) string {
	switch id {
	case "":
		return "all targets"
	case "generic_http":
		return "HTTP"
	case "generic_https":
		return "HTTPS"
	}
	label := []rune(strings.ReplaceAll(id, "_", " "))
	label[0] = unicode.ToUpper(label[0])
	return string(label)
}

// wireGbPer1k is the GB (1024³ bytes) of wire traffic (up + down) per 1,000
// completed requests; NaN without wire bytes or requests.
func wireGbPer1k(m snap.Metrics) Num {
	bytes := m.BytesUp + m.BytesDown
	if !bytes.OK() || !m.Completed.OK() || m.Completed <= 0 {
		return nan
	}
	return Num(bytes.F() / m.Completed.F() * 1000 / 1073741824)
}

// wireKbPerRequest is the KB (1024 bytes) of wire traffic per completed
// request, rounded to 2 dp: the wire bytes wireGbPer1k reads (pricing.avgPayloadKb).
// NaN without wire bytes or requests.
func wireKbPerRequest(m snap.Metrics) Num {
	gb := wireGbPer1k(m)
	if !gb.OK() || gb <= 0 {
		return nan
	}
	return Num(math.Round(gb.F()*1073741824/1000/1024*100) / 100)
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

// HistoryLabels is the old site's bucket label (format.ts bucketLabel) of
// each point of a chart answer's histories, in order.
type HistoryLabels struct {
	Success          []string `json:"success"`
	Availability     []string `json:"availability"`
	Latency          []string `json:"latency"`
	LatencyUnderLoad []string `json:"latencyUnderLoad"`
	Pool             []string `json:"pool"`
}

func seriesLabels(points []model.SeriesPoint) []string {
	out := make([]string, len(points))
	for i, p := range points {
		out[i] = bucketLabel(p.T.F())
	}
	return out
}

func latencyLabels(points []model.LatencyPoint) []string {
	out := make([]string, len(points))
	for i, p := range points {
		out[i] = bucketLabel(p.T.F())
	}
	return out
}

// label fills the chart answer's labels from its histories.
func (c *ChartAnswer) label() {
	c.Labels = HistoryLabels{Success: seriesLabels(c.Success), Availability: seriesLabels(c.Availability),
		Latency: latencyLabels(c.Latency), LatencyUnderLoad: latencyLabels(c.LatencyUnderLoad), Pool: seriesLabels(c.Pool)}
}
