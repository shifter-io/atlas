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
	meas.Performance = model.PerformanceStats{
		SuccessRate: m.SuccessRate, SuccessRateDelta24h: nan, SuccessRateDelta30d: nan, LatencyDelta24h: nan,
		LatencyP50: m.LatencyMs.P50, LatencyP90: m.LatencyMs.P90, LatencyP99: m.LatencyMs.P99, TtfbP50: nan,
		CaptchaRate: m.ChallengeRate, BlockRate: m.BlockRate, TimeoutRate: m.TimeoutRate,
		UptimePct: uptime, DowntimeMinutes24h: nan, Requests24h: day.Metrics.Completed, BandwidthMb24h: nan,
	}
	// Pricing, billing model and editorial trust fields are catalog data (site.pricing,
	// site.trust_signals) merged in by the caller; the snapshot has none of them.
	meas.Pricing = model.Pricing{PricePerGb: nan, PricePerGbAtScale: nan, MinCommit: nan, Plans: []model.PricingPlan{},
		CostPer1kSuccess: nan, AvgPayloadKb: nan, HeadlinePriceRank: nan, ValueRank: nan}
	meas.Trust = model.TrustSignals{
		ResidentialAuthenticity: nan, DatacenterAsnShare: w.Pool.DatacenterAsnShare, ClaimGap: w.Pool.DatacenterAsnShare,
		BlacklistHitRate: nan, StickinessHoldRate: nan, LeakChecks: []model.LeakCheck{},
	}
	meas.Targets = make([]model.TargetBenchmark, 0, len(targets))
	for _, t := range targets {
		tr, has := w.Targets[t.ID]
		b := model.TargetBenchmark{Target: t.ID, SuccessRate: nan, CaptchaRate: nan, BlockRate: nan, LatencyP50: nan, LatencyP90: nan, Requests: 0, Delta24h: nan}
		if has {
			r := tr.Metrics
			b.SuccessRate, b.CaptchaRate, b.BlockRate = r.SuccessRate, r.ChallengeRate, r.BlockRate
			b.LatencyP50, b.LatencyP90, b.Requests = r.LatencyMs.P50, r.LatencyMs.P90, r.Completed
		}
		meas.Targets = append(meas.Targets, b)
	}
	lat24 := []model.LatencyPoint{}
	for _, r := range points(day) {
		lat24 = append(lat24, model.LatencyPoint{T: minutesAgo(ref, r.At), P50: r.LatencyMs.P50, P90: r.LatencyMs.P90, P99: r.LatencyMs.P99})
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
		Latency30d:    pointsV(w, func(r snap.SeriesRow) Num { return r.LatencyMs.P50 }),
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
	meas.Incidents = []model.Incident{}
	mv.Measured = meas
	mv.Declared = []string{"pricing.plans", "series.pricePerGb90d", "ipSamples", "incidents", "trust.leakChecks", "overlap.likelyUpstream"}
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
	if census != nil {
		tail := ""
		if !score.Score.OK() {
			tail = " An overall score is not available yet."
		}
		mv.Verdict = fmt.Sprintf("%s The latest census covers %s: %s unique IPs from %s requests.%s",
			totals, plural(census.Countries, "country", "countries"), jsnum.Locale(census.UniqueIps), jsnum.Locale(census.Requests), tail)
	} else {
		mv.Verdict = totals + " This is one pool sample; a complete performance benchmark and overall score are not available yet."
	}
	mv.Strengths = []string{
		jsnum.Locale(w.Pool.UniqueIps.F()) + " distinct exit IPs observed",
		jsnum.Pct(m.SuccessRate.F(), 2) + " HTTP sampling success",
	}
	if census != nil {
		mv.Strengths = append(mv.Strengths, fmt.Sprintf("%s unique IPs in the latest census across %s", jsnum.Locale(census.UniqueIps), plural(census.Countries, "country", "countries")))
	}
	mv.Strengths = append(mv.Strengths, "Local DB-IP enrichment")
	mv.Weaknesses = []string{}
	if pulses == 0 {
		mv.Weaknesses = append(mv.Weaknesses, "One short run; continuous coverage unavailable")
	}
	mv.Weaknesses = append(mv.Weaknesses, untestedText+" not tested", "Pricing and billed usage not collected")
	mv.Tagline = model.KindLabels[kind] + " proxies · measured HTTP pool sample"

	mv.Score = model.ScoreView{Composite: score.Score, Rank: score.Rank, PreviousRank: score.PreviousRank, Breakdown: model.NewBreakdown(), Grade: score.Grade}
	for k, c := range score.Components {
		mv.Score.Breakdown.Set(k, c.Value)
	}
	return mv
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
