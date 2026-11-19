package build

import (
	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/snap"
)

func poolStats(w, day snap.Window) model.PoolStats {
	return model.PoolStats{
		UniqueIps24h: ipsOf(day), UniqueIps30d: ipsOf(w), ClaimedPoolSize: nan,
		ChurnRate: w.Pool.ChurnRate, RecurringShare: w.Pool.RecurringShare,
		Countries: w.Pool.Countries, Regions: w.Pool.Regions, Cities: w.Pool.Cities, Asns: w.Pool.Asns,
		RequestsPerIP: nan,
	}
}

func performanceStats(w, day snap.Window) model.PerformanceStats {
	m := w.Metrics
	uptime := nan
	if m.Uptime.OK() {
		uptime = m.Uptime * 100
	}
	lm, _ := latencyMetrics(w)
	head := headlineLatency(lm)
	return model.PerformanceStats{
		SuccessRate: m.SuccessRate, SuccessRateDelta24h: nan, SuccessRateDelta30d: nan, LatencyDelta24h: nan,
		SuccessScoreDelta24h: successScoreDelta(day), SuccessScoreDelta30d: successScoreDelta(w),
		LatencyP50: head.P50, LatencyMean: head.Mean, LatencyP90: head.P90, LatencyP99: head.P99, TtfbP50: nan,
		TimeoutRate: m.TimeoutRate,
		UptimePct:   uptime, DowntimeMinutes24h: weightedDowntimeMinutes(day.Uptime), Requests24h: day.Metrics.Completed, BandwidthMb24h: bandwidthMb(day.Metrics),
	}
}

// listPriced is whether the snapshot publishes the product's list prices.
func listPriced(p *snap.Product) bool {
	return p.Pricing != nil && (p.Pricing.LowestPerGb.OK() || p.Pricing.EntryPerGb.OK())
}

func measuredPricing(p *snap.Product, m snap.Metrics) model.Pricing {
	// Pricing, billing model and editorial trust fields are catalog data (site.pricing,
	// site.trust_signals) merged in by the caller; the snapshot has none of them.
	pricing := model.Pricing{PricePerGb: nan, PricePerGbAtScale: nan, MinCommit: nan, Plans: []model.PricingPlan{},
		BasisPerGb: nan, BasisPlanGb: nan, CostPer1kSuccess: nan, AvgPayloadKb: nan, CostPer1kAllTargets: nan,
		HeadlinePriceRank: nan, ValueRank: nan, TrialPrice: nan, TrialCredit: nan, TrialGb: nan, TrialDays: nan}
	// Every-request snapshots publish the product's list prices: the entry rate
	// (smallest plan) is pricePerGb and the lowest rate pricePerGbAtScale, the
	// site's `$ / GB` (displayRate); and the basis every cost uses (costRate).
	if listPriced(p) {
		pricing.PricePerGb, pricing.PricePerGbAtScale = p.Pricing.EntryPerGb, p.Pricing.LowestPerGb
		pricing.Model = model.S("per_gb")
	}
	if p.Pricing != nil {
		pricing.BasisPerGb, pricing.BasisPlanGb, pricing.PriceBasis = p.Pricing.BasisPerGb, p.Pricing.BasisPlanGb, p.Pricing.PriceBasis
	}
	pricing.CostPer1kSuccess = wireCostPer1k(costRate(pricing), m)
	return pricing
}

// trustSignals: in every-request snapshots (shares_basis) authenticity is the
// share of exit IPs in the requested country, the datacenter share is per IP,
// and there is no claim gap. Older snapshots keep the claim gap = datacenter share.
func trustSignals(w snap.Window) model.TrustSignals {
	trust := model.TrustSignals{
		ResidentialAuthenticity: nan, DatacenterAsnShare: w.Pool.DatacenterAsnShare, ClaimGap: w.Pool.DatacenterAsnShare,
		BlacklistHitRate: nan, StickinessHoldRate: nan, LeakChecks: []model.LeakCheck{},
	}
	if w.Pool.SharesBasis != nil {
		trust.ResidentialAuthenticity, trust.ClaimGap = w.Pool.GeoAccuracy, nan
	}
	return trust
}

func targetBenchmarks(w snap.Window, targets []model.TargetMeta) []model.TargetBenchmark {
	out := make([]model.TargetBenchmark, 0, len(targets))
	for _, t := range targets {
		tr, has := w.Targets[t.ID]
		b := model.TargetBenchmark{Target: t.ID, SuccessRate: nan, CaptchaRate: nan, BlockRate: nan, LatencyP50: nan, LatencyP90: nan, Requests: 0, Delta24h: nan}
		if has {
			r := tr.Metrics
			b.SuccessRate, b.CaptchaRate, b.BlockRate = r.SuccessRate, r.ChallengeRate, r.BlockRate
			b.Requests, b.NetworkBlocked = r.Completed, networkBlocked(w, t.ID)
			if lat, ok := targetLatency(t, tr, r); ok {
				b.LatencyP50, b.LatencyP90 = lat.P50, lat.P90
			}
		}
		// Browser cards show page loads instead (browser.go).
		if isBrowser(t, tr, has) {
			b.BrowserBenchmark, b.Scores = browserBenchmark(w, t)
		} else if has {
			b.Scores = successScores(tr.Scores)
		}
		out = append(out, b)
	}
	return out
}

// completedRows is a window's series rows that completed requests.
func completedRows(win snap.Window) []snap.SeriesRow {
	out := []snap.SeriesRow{}
	for _, r := range win.Series {
		if r.Completed > 0 {
			out = append(out, r)
		}
	}
	return out
}

// seriesPoints is v of each completed row, aged in minutes before ref.
func seriesPoints(win snap.Window, ref float64, v func(snap.SeriesRow) Num) []model.SeriesPoint {
	out := []model.SeriesPoint{}
	for _, r := range completedRows(win) {
		out = append(out, model.SeriesPoint{T: minutesAgo(ref, r.At), V: v(r)})
	}
	return out
}

func providerSeries(w, day snap.Window, ref float64) model.ProviderSeries {
	success := func(r snap.SeriesRow) Num { return r.SuccessRate }
	age := func(at string) Num { return minutesAgo(ref, at) }
	lat24 := []model.LatencyPoint{}
	for _, r := range completedRows(day) {
		l := seriesLatency(day, r)
		lat24 = append(lat24, model.LatencyPoint{T: minutesAgo(ref, r.At), P50: l.P50, P90: l.P90, P99: l.P99})
	}
	return model.ProviderSeries{
		Success24h: seriesPoints(day, ref, success), Success30d: seriesPoints(w, ref, success), Latency24h: lat24,
		Latency30d:    seriesPoints(w, ref, func(r snap.SeriesRow) Num { return seriesLatency(w, r).P50 }),
		UniqueIps24h:  ipPoints(day, "24h", completedRows(day), age),
		UniqueIps30d:  ipPoints(w, "30d", completedRows(w), age),
		PricePerGb90d: []model.SeriesPoint{},
	}
}

func geoBuckets(w snap.Window) []model.GeoBucket {
	byCountry := map[string]snap.BreakdownRow{}
	for _, r := range w.Breakdowns["country"] {
		if r.CountryCode != nil {
			byCountry[*r.CountryCode] = r
		}
	}
	out := []model.GeoBucket{}
	for _, g := range w.Pool.CountriesBreakdown {
		b := model.GeoBucket{CountryCode: g.CountryCode, Ips: scaledIps(w, g.Ips), Share: scaledShare(w, g.Ips), LatencyP50: nan, SuccessRate: nan}
		if r, has := byCountry[g.CountryCode]; has {
			b.LatencyP50, b.SuccessRate = r.P50, r.SuccessRate
		}
		out = append(out, b)
	}
	return out
}

// asnKind maps a pool ASN usage type to the site's ASN kind.
func asnKind(usageType string) string {
	switch usageType {
	case "hosting":
		return "hosting"
	case "cellular":
		return "mobile"
	case "business":
		return "business"
	}
	return "eyeball"
}

func asnBuckets(w snap.Window) []model.AsnBucket {
	out := []model.AsnBucket{}
	for _, a := range w.Pool.AsnsBreakdown {
		out = append(out, model.AsnBucket{Asn: a.Asn, Name: model.S(a.Name), Kind: asnKind(a.UsageType), Ips: scaledIps(w, a.Ips), Share: scaledShare(w, a.Ips)})
	}
	return out
}

// overlapSummary: presentation.ts leaves the overlap block unavailable. The snapshot does publish the
// 30d exclusive/shared share and per-pair counts, so the API maps them when present.
func overlapSummary(w snap.Window, peers []model.PoolOverlap) model.OverlapSummary {
	o := model.OverlapSummary{ExclusiveShare: w.Overlap.ExclusiveShare, SharedShare: w.Overlap.SharedShare, Overlaps: peers}
	if o.Overlaps == nil {
		o.Overlaps = []model.PoolOverlap{}
	}
	return o
}

// declaredNA is the N/A paths the builder declares explicitly.
func declaredNA(p *snap.Product, peers []model.PoolOverlap) []string {
	declared := []string{"pricing.plans", "series.pricePerGb90d", "ipSamples", "trust.leakChecks", "overlap.likelyUpstream"}
	if p.Outages == nil {
		// A v1 snapshot publishes no outages: unknown, not zero.
		declared = append(declared, "incidents")
	}
	if len(peers) == 0 {
		declared = append(declared, "overlap.overlaps")
	}
	return declared
}
