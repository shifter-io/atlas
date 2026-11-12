package build

import (
	"fmt"
	"math"
	"sort"
	"strings"

	"github.com/Zeus-Development/proxy-index/api/internal/jsnum"
	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/Zeus-Development/proxy-index/api/internal/snap"
)

// measuredView is the result of porting presentation.ts measuredProvider for
// one snapshot product: the measurement block plus the generated copy.
type measuredView struct {
	Kind       string
	Product    string
	Measured   model.Measured
	Score      model.ScoreView
	Tagline    string
	Verdict    string
	Strengths  []string
	Weaknesses []string
	// NA paths the builder declares explicitly (empty-but-not-collected lists).
	Declared []string
}

func plural(n float64, one, many string) string {
	word := many
	if n == 1 {
		word = one
	}
	return jsnum.Locale(n) + " " + word
}

// censusTotals is census.ts censusTotals/productCensus.
type censusTotals struct {
	Countries float64
	UniqueIps float64
	Requests  float64
}

func productCensus(p *snap.Product) *censusTotals {
	if len(p.Census) == 0 {
		return nil
	}
	seen := map[string]bool{}
	t := &censusTotals{}
	for _, c := range p.Census {
		seen[strings.ToUpper(c.Country)] = true
		t.UniqueIps += c.UniqueIps.F()
		t.Requests += c.Requests.F()
	}
	t.Countries = float64(len(seen))
	return t
}

// proxyKind maps a snapshot product kind to the site's category.
func proxyKind(kind string) string { return snap.ProxyKind(kind) }

// minutesAgo is the age of a stamp in minutes before the reference time.
// presentation.ts divides milliseconds by 60 here, which is not minutes; the
// SeriesPoint contract says minutes, so the API publishes minutes
// (documented in FIELDS.md).
func minutesAgo(refMs float64, stamp string) Num {
	return Num((refMs - snap.Ms(stamp)) / 60000)
}

// measuredProvider ports presentation.ts measuredProvider for one product.
// overlapPeers supplies the 30d snapshot overlap rows for this product.
func measuredProvider(p *snap.Product, s *snap.Snapshot, targets []model.TargetMeta, peers []model.PoolOverlap) measuredView {
	kind := proxyKind(p.Kind)
	mv := measuredView{Kind: kind, Product: model.DefaultProduct[kind]}
	mv.Measured = measuredBlock(p, snap.Ms(s.AsOf), targets, peers)
	mv.Declared = declaredNA(p, peers)
	score := balancedScore(p.Window("30d"))
	writeMeasuredCopy(&mv, p, score)
	mv.Score = scoreView(score)
	return mv
}

// measuredBlock is the measurement block of one product, aged against ref.
func measuredBlock(p *snap.Product, ref float64, targets []model.TargetMeta, peers []model.PoolOverlap) model.Measured {
	w := p.Window("30d")
	day := p.Window("24h")
	var meas model.Measured
	meas.Pool = poolStats(w, day)
	meas.Performance = performanceStats(w, day)
	meas.Pricing = measuredPricing(p, w.Metrics)
	meas.Trust = trustSignals(w)
	meas.Targets = targetBenchmarks(w, targets)
	meas.BrowserOverall = browserOverall(w)
	meas.SuccessScore = successScore(w)
	meas.Series = providerSeries(w, day, ref)
	meas.Geo = geoBuckets(w)
	meas.Asns = asnBuckets(w)
	meas.Overlap = overlapSummary(w, peers)
	meas.IPSamples = []model.IPSample{}
	countries := nan
	if w.Uptime != nil {
		countries = w.Uptime.Countries
	}
	meas.Incidents = outageIncidents(p, ref, countries)
	return meas
}

func balancedScore(w snap.Window) snap.Score {
	if score, ok := w.Scores["balanced"]; ok {
		return score
	}
	return snap.EmptyScore()
}

func scoreView(score snap.Score) model.ScoreView {
	v := model.ScoreView{Composite: score.Score, Rank: score.Rank, PreviousRank: score.PreviousRank, Breakdown: model.NewBreakdown(), Grade: score.Grade}
	if peers := score.Peers(); peers.OK() {
		v.Peers = peers
	}
	for k, c := range score.Components {
		v.Breakdown.Set(k, c.Value)
	}
	return v
}

// writeMeasuredCopy writes the generated copy. It follows the data: the
// schedule the network is measured on, its score once there is one, and only
// what is really still missing.
func writeMeasuredCopy(mv *measuredView, p *snap.Product, score snap.Score) {
	w := p.Window("30d")
	census := productCensus(p)
	pulses := 0.0
	if w.Uptime != nil && w.Uptime.Batches.OK() {
		pulses = w.Uptime.Batches.F()
	}
	mv.Verdict = verdict(w, census, score)
	mv.Strengths = strengths(w, census)
	mv.Weaknesses = weaknesses(p, w, pulses)
	mv.Tagline = model.KindLabels[mv.Kind] + " proxies · " + measuredSchedule(p)
}

func verdict(w snap.Window, census *censusTotals, score snap.Score) string {
	m := w.Metrics
	totals := fmt.Sprintf("%s completed requests, %s successes and %s unique IPs.",
		jsnum.Count(m.Completed.F()), jsnum.Count(m.Successes.F()), jsnum.Count(ipsOf(w).F()))
	if census == nil {
		return totals + " This is one pool sample; a complete performance benchmark and overall score are not available yet."
	}
	tail := " An overall score is not available yet."
	if score.Score.OK() {
		tail = " MILF Score " + jsnum.ToFixed(score.Score.F(), 1)
		if score.Grade != nil {
			tail += " (" + *score.Grade + ")"
		}
		if score.Peers() == 1 {
			tail += ", alone in its kind so far: every part is scored against itself"
		}
		tail += "."
	}
	return fmt.Sprintf("%s The latest census covers %s: %s unique IPs from %s requests.%s",
		totals, plural(census.Countries, "country", "countries"), jsnum.Locale(census.UniqueIps), jsnum.Locale(census.Requests), tail)
}

func strengths(w snap.Window, census *censusTotals) []string {
	success := " HTTP sampling success"
	if w.Metrics.EveryRequest() {
		success = " time-weighted success over every request"
	}
	out := []string{
		jsnum.Count(ipsOf(w).F()) + ipsPhrase(w),
		jsnum.Pct(w.Metrics.SuccessRate.F(), 2) + success,
	}
	if census != nil {
		out = append(out, fmt.Sprintf("%s unique IPs in the latest census across %s", jsnum.Locale(census.UniqueIps), plural(census.Countries, "country", "countries")))
	}
	return append(out, "Local DB-IP enrichment")
}

func weaknesses(p *snap.Product, w snap.Window, pulses float64) []string {
	out := []string{}
	if pulses == 0 {
		out = append(out, "One short run; continuous coverage unavailable")
	}
	untested := untestedText(w)
	out = append(out, strings.ToUpper(untested[:1])+untested[1:]+" not tested")
	if listPriced(p) {
		return append(out, "Billed usage not collected (list prices only)")
	}
	return append(out, "Pricing and billed usage not collected")
}

// untestedText lists what the window did not test: "HTTPS, uptime and sticky sessions".
func untestedText(w snap.Window) string {
	untested := []string{}
	if t, has := w.Targets["generic_https"]; !has || !t.Metrics.Completed.OK() || t.Metrics.Completed <= 0 {
		untested = append(untested, "HTTPS")
	}
	if !w.Metrics.Uptime.OK() {
		untested = append(untested, "uptime")
	}
	untested = append(untested, "sticky sessions")
	if len(untested) == 1 {
		return untested[0]
	}
	return strings.Join(untested[:len(untested)-1], ", ") + " and " + untested[len(untested)-1]
}

// outageIncidents ports presentation.ts outageIncidents: each outage the
// snapshot publishes (v2 `outages`) as an Incident, aged against as_of. An
// outage takes one of the `countries` the product was tested in over 30 days
// out, so its impact is 1/countries, the weight the framework's time-based
// uptime gives it. A trough success rate is not published (N/A).
func outageIncidents(p *snap.Product, refMs float64, countries Num) []model.Incident {
	out := []model.Incident{}
	impact := nan
	if countries.OK() && countries > 0 {
		impact = 1 / countries
	}
	for _, o := range p.Outages {
		summary := "Down in " + o.Country + ", detected by " + o.Source
		if o.EndedAt == nil {
			summary += ", still ongoing"
		}
		out = append(out, model.Incident{ID: "outage:" + p.ID + ":" + o.Country + ":" + o.StartedAt,
			StartedAgoSeconds: Num((refMs - snap.Ms(o.StartedAt)) / 1000), DurationMinutes: o.DurationS / 60,
			Severity: model.S("major"), Scope: model.S(o.Country), Summary: model.S(summary + "."),
			TroughSuccessRate: nan, Impact: impact})
	}
	return out
}

// weightedDowntimeMinutes ports window.ts weightedDowntimeMinutes: the
// framework's outage time over the countries tested, in minutes (1 dp), so
// that uptime = 1 - minutes / window minutes, like the capacity-weighted
// downtime of the demo networks. NaN without time-based uptime.
func weightedDowntimeMinutes(up *snap.Uptime) Num {
	if up == nil || up.Basis == nil || *up.Basis != "outage time" || !up.DowntimeS.OK() || !up.Countries.OK() || up.Countries <= 0 {
		return nan
	}
	return Num(jsnum.FixedNum(up.DowntimeS.F()/up.Countries.F()/60, 1))
}

var naLatency = snap.Latency{Mean: nan, P50: nan, P90: nan, P95: nan, P99: nan}

// latencyTarget is the only target whose latency the site shows (product
// rule): plain HTTP. HTTPS adds CONNECT + TLS round trips, about twice as
// slow, so mixing them would make "latency" depend on the target mix.
const latencyTarget = "generic_http"

// latencyMetrics ports every-request.ts latencyMetrics: the latency target's
// metrics and its id, or N/A latency and "" when the window has no such target
// (the window's own latency mixes targets and is never used instead).
func latencyMetrics(w snap.Window) (snap.Metrics, string) {
	if t, ok := w.Targets[latencyTarget]; ok {
		return t.Metrics, latencyTarget
	}
	return snap.Metrics{LatencyMs: naLatency}, ""
}

// windowLatencyIsHTTP ports every-request.ts windowLatencyIsHttp: whether a
// window's own latency (its series and breakdowns) is the latency target's
// alone, so charts may show it. Every-request snapshots say which targets
// voted (time_slots.latency_targets); otherwise no other target may have
// requests in the window.
func windowLatencyIsHTTP(w snap.Window) bool {
	if ts := w.Metrics.TimeSlots; ts != nil && ts.LatencyTargets != nil {
		for _, t := range ts.LatencyTargets {
			if t != latencyTarget {
				return false
			}
		}
		return true
	}
	for id, t := range w.Targets {
		// Browser targets have no latency: they never vote (page loads instead).
		if id != latencyTarget && t.Browser == nil && t.Metrics.Completed > 0 {
			return false
		}
	}
	return true
}

// seriesLatency is a series row's latency where the window's latency is the
// latency target's, else N/A.
func seriesLatency(w snap.Window, r snap.SeriesRow) snap.Latency {
	if windowLatencyIsHTTP(w) {
		return r.LatencyMs
	}
	return naLatency
}

// underLoad reports whether m's headline latency is latency under load: an
// every-request snapshot's window with a load test.
func underLoad(m snap.Metrics) bool {
	return m.EveryRequest() && m.LatencyUnderLoadMs != nil && m.LatencyUnderLoadMs.P50.OK()
}

// notLatencyTarget is the calculator's latency_reason of a target whose
// latency it does not measure (calculated/scores.py NOT_LATENCY_TARGET).
const notLatencyTarget = "Latency is measured on the latency target only"

// targetLatency is the latency a target's entry shows, and whether it has
// one: the latency target's headline latency; another HTTP target's
// display-only light-load latency of its own successes (HTTPS-LATENCY-1),
// in every-request snapshots only. Browser targets show page loads instead.
func targetLatency(t model.TargetMeta, tr snap.TargetResult, m snap.Metrics) (snap.Latency, bool) {
	if t.ID == latencyTarget {
		return headlineLatency(m), true
	}
	return m.LatencyMs, m.EveryRequest() && !isBrowser(t, tr, true)
}

// fewSuccesses reports whether a target's latency is null because no time
// slot had min_latency_successes light-load successes: the calculator
// measures it (it gave a reason other than notLatencyTarget) but found too
// few.
func fewSuccesses(m snap.Metrics, p50 Num) bool {
	return m.EveryRequest() && !p50.OK() && m.LatencyReason != nil && *m.LatencyReason != notLatencyTarget
}

// headlineLatency ports presentation.ts headlineLatency: the latency the site
// leads with. Every-request snapshots: latency under load, else (no load test
// in the window: production runs none, docs/api-web-map.md section 14) the
// light-load latency of its own tests, the daily tests'. Older snapshots:
// latency_ms, their only latency.
func headlineLatency(m snap.Metrics) snap.Latency {
	if underLoad(m) {
		return *m.LatencyUnderLoadMs
	}
	return m.LatencyMs
}

// lightLatency is the light-load latency shown next to a headline latency
// under load; N/A otherwise (it is then the headline, or there is none).
func lightLatency(m snap.Metrics) snap.Latency {
	if !underLoad(m) {
		return naLatency
	}
	return m.LatencyMs
}

// listRate ports format.ts listRate: the `$ / GB` the site shows, the lowest
// list rate of the tested pool (pricePerGbAtScale), else the entry rate.
func listRate(p model.Pricing) Num {
	if p.PricePerGbAtScale.OK() {
		return p.PricePerGbAtScale
	}
	return p.PricePerGb
}

// wireCostPer1k ports window.ts wireCostPer1k: dollars per 1,000 successful
// requests at `price` per GB from a window's wire bytes (up + down) per request
// and its request success rate (successes ÷ requests; success_rate in older
// snapshots), unrounded, at GB = 10^9 bytes (VALUE-ALL-2). NaN without a
// price, wire bytes or successes.
func wireCostPer1k(price Num, m snap.Metrics) Num {
	rate := m.RequestSuccessRate
	if !rate.OK() {
		rate = m.SuccessRate
	}
	gb := wireGbPer1k(m)
	if !price.OK() || !gb.OK() || !rate.OK() || rate <= 0 {
		return nan
	}
	return Num(gb.F() * price.F() / rate.F())
}

func costBasisText(price Num, m snap.Metrics) string {
	rate := m.RequestSuccessRate
	if !rate.OK() {
		rate = m.SuccessRate
	}
	gb := (m.BytesUp + m.BytesDown).F() / m.Completed.F() * 1000 / gbBytes
	return fmt.Sprintf("%s GB of wire traffic per 1,000 requests at $%s/GB (lowest list rate), divided by a %s request success rate.",
		jsnum.ToFixed(gb, 3), jsnum.ToFixed(price.F(), 2), jsnum.Pct(rate.F(), 1))
}

// overlapPeers maps the snapshot's 30d pair rows for one product into PoolOverlap entries.
func overlapPeers(s *snap.Snapshot, p *snap.Product, slugOf map[int]string, nameOf map[string]string) []model.PoolOverlap {
	out := []model.PoolOverlap{}
	id := int(p.ProductID.F())
	for _, r := range s.Overlap {
		if r.Window != "30d" || r.Cohort != p.Cohort {
			continue
		}
		a, b := int(r.ProductA.F()), int(r.ProductB.F())
		var other int
		var ours, theirs Num
		switch id {
		case a:
			other, ours, theirs = b, r.IpsA, r.IpsB
		case b:
			other, ours, theirs = a, r.IpsB, r.IpsA
		default:
			continue
		}
		slug, ok := slugOf[other]
		if !ok {
			continue
		}
		name := nameOf[slug]
		out = append(out, model.PoolOverlap{Slug: slug, Name: model.S(name), SharedIps: r.SharedIps,
			ShareOfOurs: r.SharedIps / ours, ShareOfTheirs: r.SharedIps / theirs, RequestsBothSides: nan})
	}
	sort.SliceStable(out, func(i, j int) bool { return out[i].SharedIps > out[j].SharedIps })
	return out
}

// costPer1k derives cost per 1,000 successful requests from catalog pricing and
// the measured success rate, the way the demo's costBasis describes it.
func costPer1k(pricePerGb, avgPayloadKb, successRate Num) (Num, string) {
	if !pricePerGb.OK() || !avgPayloadKb.OK() || !successRate.OK() || successRate <= 0 {
		return nan, ""
	}
	gb := avgPayloadKb.F() * 1000 / 1048576
	cost := gb * pricePerGb.F() / successRate.F()
	basis := fmt.Sprintf("%s GB per 1,000 requests at $%s/GB, divided by a %s success rate.",
		jsnum.ToFixed(gb, 3), jsnum.ToFixed(pricePerGb.F(), 2), jsnum.Pct(successRate.F(), 1))
	return Num(math.Round(cost*10000) / 10000), basis
}
