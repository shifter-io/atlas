package build

import (
	"fmt"
	"math"
	"sort"
	"strings"

	"github.com/ice-zeus/milf-website/api/internal/jsnum"
	"github.com/ice-zeus/milf-website/api/internal/model"
	"github.com/ice-zeus/milf-website/api/internal/snap"
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
func proxyKind(kind string) string {
	if model.IsProxyKind(kind) {
		return kind
	}
	return "residential"
}

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
	w := p.Window("30d")
	day := p.Window("24h")
	m := w.Metrics
	score, ok := w.Scores["balanced"]
	if !ok {
		score = snap.EmptyScore()
	}
	ref := snap.Ms(s.AsOf)
	points := func(win snap.Window) []snap.SeriesRow {
		out := []snap.SeriesRow{}
		for _, r := range win.Series {
			if r.Completed > 0 {
				out = append(out, r)
			}
		}
		return out
	}
	successes := func(win snap.Window) []model.SeriesPoint {
		out := []model.SeriesPoint{}
		for _, r := range points(win) {
			out = append(out, model.SeriesPoint{T: minutesAgo(ref, r.At), V: r.SuccessRate})
		}
		return out
	}
	kind := proxyKind(p.Kind)
	product := model.DefaultProduct[kind]
	mv := measuredView{Kind: kind, Product: product}
	var meas model.Measured
	meas.Pool = model.PoolStats{
		UniqueIps24h: day.Pool.UniqueIps, UniqueIps30d: w.Pool.UniqueIps, ClaimedPoolSize: nan,
		ChurnRate: w.Pool.ChurnRate, RecurringShare: w.Pool.RecurringShare,
		Countries: w.Pool.Countries, Regions: w.Pool.Regions, Cities: w.Pool.Cities, Asns: w.Pool.Asns,
		RequestsPerIP: nan,
	}
	uptime := nan
	if m.Uptime.OK() {
		uptime = m.Uptime * 100
	}
	lm, _ := latencyMetrics(w)
	head := headlineLatency(lm)
	meas.Performance = model.PerformanceStats{
		SuccessRate: m.SuccessRate, SuccessRateDelta24h: nan, SuccessRateDelta30d: nan, LatencyDelta24h: nan,
		LatencyP50: head.P50, LatencyP90: head.P90, LatencyP99: head.P99, TtfbP50: nan,
		CaptchaRate: m.ChallengeRate, BlockRate: m.BlockRate, TimeoutRate: m.TimeoutRate,
		UptimePct: uptime, DowntimeMinutes24h: nan, Requests24h: day.Metrics.Completed, BandwidthMb24h: nan,
	}
	// Pricing, billing model and editorial trust fields are catalog data (site.pricing,
	// site.trust_signals) merged in by the caller; the snapshot has none of them.
	meas.Pricing = model.Pricing{PricePerGb: nan, PricePerGbAtScale: nan, MinCommit: nan, Plans: []model.PricingPlan{},
		CostPer1kSuccess: nan, AvgPayloadKb: nan, HeadlinePriceRank: nan, ValueRank: nan}
	// Every-request snapshots publish the product's list prices: the entry rate
	// (smallest plan) is pricePerGb and the lowest rate pricePerGbAtScale, the
	// site's `$ / GB` (listRate).
	if pr := p.Pricing; pr != nil && (pr.LowestPerGb.OK() || pr.EntryPerGb.OK()) {
		meas.Pricing.PricePerGb, meas.Pricing.PricePerGbAtScale = pr.EntryPerGb, pr.LowestPerGb
		meas.Pricing.Model = model.S("per_gb")
	}
	meas.Pricing.CostPer1kSuccess = wireCostPer1k(listRate(meas.Pricing), m)
	if meas.Pricing.CostPer1kSuccess.OK() {
		meas.Pricing.CostBasis = model.S(costBasisText(listRate(meas.Pricing), m))
	}
	// Every-request snapshots (shares_basis): authenticity is the share of exit
	// IPs in the requested country, the datacenter share is per IP, and there
	// is no claim gap. Older snapshots keep the claim gap = datacenter share.
	meas.Trust = model.TrustSignals{
		ResidentialAuthenticity: nan, DatacenterAsnShare: w.Pool.DatacenterAsnShare, ClaimGap: w.Pool.DatacenterAsnShare,
		BlacklistHitRate: nan, StickinessHoldRate: nan, LeakChecks: []model.LeakCheck{},
	}
	if w.Pool.SharesBasis != nil {
		meas.Trust.ResidentialAuthenticity, meas.Trust.ClaimGap = w.Pool.GeoAccuracy, nan
	}
	meas.Targets = make([]model.TargetBenchmark, 0, len(targets))
	for _, t := range targets {
		tr, has := w.Targets[t.ID]
		b := model.TargetBenchmark{Target: t.ID, SuccessRate: nan, CaptchaRate: nan, BlockRate: nan, LatencyP50: nan, LatencyP90: nan, Requests: 0, Delta24h: nan}
		if has {
			r := tr.Metrics
			b.SuccessRate, b.CaptchaRate, b.BlockRate = r.SuccessRate, r.ChallengeRate, r.BlockRate
			b.Requests = r.Completed
			// Latency is shown for the latency target only.
			if t.ID == latencyTarget {
				lat := headlineLatency(r)
				b.LatencyP50, b.LatencyP90 = lat.P50, lat.P90
			}
		}
		meas.Targets = append(meas.Targets, b)
	}
	lat24 := []model.LatencyPoint{}
	for _, r := range points(day) {
		l := seriesLatency(day, r)
		lat24 = append(lat24, model.LatencyPoint{T: minutesAgo(ref, r.At), P50: l.P50, P90: l.P90, P99: l.P99})
	}
	pointsV := func(win snap.Window, v func(snap.SeriesRow) Num) []model.SeriesPoint {
		out := []model.SeriesPoint{}
		for _, r := range points(win) {
			out = append(out, model.SeriesPoint{T: minutesAgo(ref, r.At), V: v(r)})
		}
		return out
	}
	meas.Series = model.ProviderSeries{
		Success24h: successes(day), Success30d: successes(w), Latency24h: lat24,
		Latency30d:    pointsV(w, func(r snap.SeriesRow) Num { return seriesLatency(w, r).P50 }),
		UniqueIps24h:  pointsV(day, func(r snap.SeriesRow) Num { return r.UniqueIps }),
		UniqueIps30d:  pointsV(w, func(r snap.SeriesRow) Num { return r.UniqueIps }),
		PricePerGb90d: []model.SeriesPoint{},
	}
	byCountry := map[string]snap.BreakdownRow{}
	for _, r := range w.Breakdowns["country"] {
		if r.CountryCode != nil {
			byCountry[*r.CountryCode] = r
		}
	}
	meas.Geo = []model.GeoBucket{}
	for _, g := range w.Pool.CountriesBreakdown {
		b := model.GeoBucket{CountryCode: g.CountryCode, Country: model.S(g.Country), Ips: g.Ips, Share: g.Ips / w.Pool.UniqueIps, LatencyP50: nan, SuccessRate: nan}
		if r, has := byCountry[g.CountryCode]; has {
			b.LatencyP50, b.SuccessRate = r.P50, r.SuccessRate
		}
		meas.Geo = append(meas.Geo, b)
	}
	meas.Asns = []model.AsnBucket{}
	for _, a := range w.Pool.AsnsBreakdown {
		k := "eyeball"
		switch a.UsageType {
		case "hosting":
			k = "hosting"
		case "cellular":
			k = "mobile"
		case "business":
			k = "business"
		}
		meas.Asns = append(meas.Asns, model.AsnBucket{Asn: a.Asn, Name: model.S(a.Name), Kind: k, Ips: a.Ips, Share: a.Ips / w.Pool.UniqueIps})
	}
	// presentation.ts leaves the overlap block unavailable. The snapshot does publish the
	// 30d exclusive/shared share and per-pair counts, so the API maps them when present.
	meas.Overlap = model.OverlapSummary{ExclusiveShare: w.Overlap.ExclusiveShare, SharedShare: w.Overlap.SharedShare, Overlaps: peers}
	if meas.Overlap.Overlaps == nil {
		meas.Overlap.Overlaps = []model.PoolOverlap{}
	}
	meas.IPSamples = []model.IPSample{}
	countries := nan
	if w.Uptime != nil {
		countries = w.Uptime.Countries
	}
	meas.Incidents = outageIncidents(p, ref, countries)
	meas.Performance.DowntimeMinutes24h = weightedDowntimeMinutes(day.Uptime)
	mv.Measured = meas
	mv.Declared = []string{"pricing.plans", "series.pricePerGb90d", "ipSamples", "trust.leakChecks", "overlap.likelyUpstream"}
	if p.Outages == nil {
		// A v1 snapshot publishes no outages: unknown, not zero.
		mv.Declared = append(mv.Declared, "incidents")
	}
	if len(peers) == 0 {
		mv.Declared = append(mv.Declared, "overlap.overlaps")
	}

	census := productCensus(p)
	pulses := 0.0
	if w.Uptime != nil && w.Uptime.Batches.OK() {
		pulses = w.Uptime.Batches.F()
	}
	httpsMeasured := false
	if t, has := w.Targets["generic_https"]; has && t.Metrics.Completed.OK() && t.Metrics.Completed > 0 {
		httpsMeasured = true
	}
	untested := []string{}
	if !httpsMeasured {
		untested = append(untested, "HTTPS")
	}
	if !m.Uptime.OK() {
		untested = append(untested, "uptime")
	}
	untested = append(untested, "sticky sessions")
	untestedText := untested[0]
	if len(untested) > 1 {
		untestedText = strings.Join(untested[:len(untested)-1], ", ") + " and " + untested[len(untested)-1]
	}
	totals := fmt.Sprintf("%s completed requests, %s successes and %s unique IPs.",
		jsnum.Locale(m.Completed.F()), jsnum.Locale(m.Successes.F()), jsnum.Locale(w.Pool.UniqueIps.F()))
	// The copy follows the data: the schedule the network is measured on, its
	// score once there is one, and only what is really still missing.
	scheduled := census != nil || pulses > 0
	priced := p.Pricing != nil && (p.Pricing.LowestPerGb.OK() || p.Pricing.EntryPerGb.OK())
	if census != nil {
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
		mv.Verdict = fmt.Sprintf("%s The latest census covers %s: %s unique IPs from %s requests.%s",
			totals, plural(census.Countries, "country", "countries"), jsnum.Locale(census.UniqueIps), jsnum.Locale(census.Requests), tail)
	} else {
		mv.Verdict = totals + " This is one pool sample; a complete performance benchmark and overall score are not available yet."
	}
	success := " HTTP sampling success"
	if m.EveryRequest() {
		success = " time-weighted success over every request"
	}
	mv.Strengths = []string{
		jsnum.Locale(w.Pool.UniqueIps.F()) + " distinct exit IPs observed",
		jsnum.Pct(m.SuccessRate.F(), 2) + success,
	}
	if census != nil {
		mv.Strengths = append(mv.Strengths, fmt.Sprintf("%s unique IPs in the latest census across %s", jsnum.Locale(census.UniqueIps), plural(census.Countries, "country", "countries")))
	}
	mv.Strengths = append(mv.Strengths, "Local DB-IP enrichment")
	mv.Weaknesses = []string{}
	if pulses == 0 {
		mv.Weaknesses = append(mv.Weaknesses, "One short run; continuous coverage unavailable")
	}
	mv.Weaknesses = append(mv.Weaknesses, strings.ToUpper(untestedText[:1])+untestedText[1:]+" not tested")
	if priced {
		mv.Weaknesses = append(mv.Weaknesses, "Billed usage not collected (list prices only)")
	} else {
		mv.Weaknesses = append(mv.Weaknesses, "Pricing and billed usage not collected")
	}
	switch {
	case m.EveryRequest():
		mv.Tagline = model.KindLabels[kind] + " proxies · measured on a census, pulse and load schedule"
	case scheduled:
		mv.Tagline = model.KindLabels[kind] + " proxies · measured on a census and pulse schedule"
	default:
		mv.Tagline = model.KindLabels[kind] + " proxies · measured HTTP pool sample"
	}

	mv.Score = model.ScoreView{Composite: score.Score, Rank: score.Rank, PreviousRank: score.PreviousRank, Breakdown: model.NewBreakdown(), Grade: score.Grade}
	if peers := score.Peers(); peers.OK() {
		mv.Score.Peers = peers
	}
	for k, c := range score.Components {
		mv.Score.Breakdown.Set(k, c.Value)
	}
	return mv
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

var naLatency = snap.Latency{P50: nan, P90: nan, P95: nan, P99: nan}

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
		if id != latencyTarget && t.Metrics.Completed > 0 {
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

// headlineLatency ports presentation.ts headlineLatency: the latency the site
// leads with. Every-request snapshots: latency under load (N/A without a load
// test in the window). Older snapshots: latency_ms, their only latency.
func headlineLatency(m snap.Metrics) snap.Latency {
	if !m.EveryRequest() {
		return m.LatencyMs
	}
	if m.LatencyUnderLoadMs == nil {
		return naLatency
	}
	return *m.LatencyUnderLoadMs
}

// lightLatency is an every-request snapshot's light-load latency (pulses
// only), shown next to the headline; N/A for older snapshots.
func lightLatency(m snap.Metrics) snap.Latency {
	if !m.EveryRequest() {
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
// snapshots), rounded to 4 dp. NaN without a price, wire bytes or successes.
func wireCostPer1k(price Num, m snap.Metrics) Num {
	rate := m.RequestSuccessRate
	if !rate.OK() {
		rate = m.SuccessRate
	}
	bytes := m.BytesUp + m.BytesDown
	if !price.OK() || !bytes.OK() || !m.Completed.OK() || m.Completed <= 0 || !rate.OK() || rate <= 0 {
		return nan
	}
	gb := float64(bytes.F() / m.Completed.F() * 1000 / 1073741824)
	return Num(math.Round(float64(gb*price.F()/rate.F())*10000) / 10000)
}

func costBasisText(price Num, m snap.Metrics) string {
	rate := m.RequestSuccessRate
	if !rate.OK() {
		rate = m.SuccessRate
	}
	gb := (m.BytesUp + m.BytesDown).F() / m.Completed.F() * 1000 / 1073741824
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
