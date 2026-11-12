package build

import (
	"sort"
	"strings"

	"github.com/Zeus-Development/proxy-index/api/internal/jsnum"
	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/Zeus-Development/proxy-index/api/internal/snap"
)

// Standing is a ranking row's place in its kind by every-request.ts
// kindStanding: the framework's rank, or 1 for a scored network alone in its
// kind ("#1 of 1"); Peers is the ready networks of the kind.
type Standing struct {
	Rank  Num `json:"rank"`
	Peers Num `json:"peers"`
}

// RowPricing is a ranking row's list prices and trial (site.pricing, or the
// tested pool's list rates).
type RowPricing struct {
	// ListRate is format.ts listRate: pricePerGbAtScale, else pricePerGb.
	ListRate          Num `json:"listRate"`
	PricePerGb        Num `json:"pricePerGb"`
	PricePerGbAtScale Num `json:"pricePerGbAtScale"`
	// PriceLabel is format.ts priceRange of the two rates, as a product's.
	PriceLabel model.Str `json:"priceLabel"`
	Trial      model.Str `json:"trial"`
	// HasTrial is true when the trial text is not "none"; null without one.
	HasTrial  model.Bool `json:"hasTrial"`
	TrialKind model.Str  `json:"trialKind"`
}

// RowTrust is a ranking row's trust signals (the provider's trust block).
type RowTrust struct {
	Authenticity         Num        `json:"authenticity"`
	KycVerified          model.Bool `json:"kycVerified"`
	ConsentDisclosed     model.Bool `json:"consentDisclosed"`
	StickinessAdvertised model.Str  `json:"stickinessAdvertised"`
	StickinessHoldRate   Num        `json:"stickinessHoldRate"`
	BlacklistHitRate     Num        `json:"blacklistHitRate"`
}

// RowBrowserScore is an engine's overall browser success score on a ranking
// row, without the targets and weights the network answer gives.
type RowBrowserScore struct {
	Success model.MetricScore `json:"success"`
}

// TargetCounts counts the dataset's targets: every one, the measured ones
// and the cards (isCard). /v1/meta and /v1/rankings both count with
// targetCounts, so their counts agree.
type TargetCounts struct {
	All      int `json:"all"`
	Measured int `json:"measured"`
	Cards    int `json:"cards"`
}

// RankingFleet is the range's requests and successful requests summed over
// the networks listed.
type RankingFleet struct {
	Requests  Num `json:"requests"`
	Successes Num `json:"successes"`
}

// CostEnd is the cheapest or dearest network of a cost summary.
type CostEnd struct {
	Slug             string `json:"slug"`
	Name             string `json:"name"`
	CostPer1kSuccess Num    `json:"costPer1kSuccess"`
}

// CostSummary is the true-cost page's summary of the listed networks' cost
// per 1,000 successful requests: the cheapest, the median, the dearest, the
// dearest ÷ cheapest spread and how many networks are priced.
type CostSummary struct {
	Cheapest *CostEnd `json:"cheapest"`
	Median   Num      `json:"median"`
	Dearest  *CostEnd `json:"dearest"`
	SpreadX  Num      `json:"spreadX"`
	Priced   int      `json:"priced"`
}

// RankingsCost is the rankings' cost summary: over costPer1kSuccess, and
// AllTargets the same over costPer1kAllTargets (VALUE-ALL-2; its ends'
// costPer1kSuccess holds the 8-target bill).
type RankingsCost struct {
	CostSummary
	AllTargets CostSummary `json:"allTargets"`
}

func rowPricing(p model.Pricing) RowPricing {
	out := RowPricing{ListRate: listRate(p), PricePerGb: p.PricePerGb, PricePerGbAtScale: p.PricePerGbAtScale, Trial: p.Trial,
		TrialKind: trialKind(p.Trial)}
	if p.PricePerGb.OK() || p.PricePerGbAtScale.OK() {
		out.PriceLabel = model.S(jsnum.PriceRange(p.PricePerGbAtScale.F(), p.PricePerGb.F()))
	}
	if out.TrialKind != nil {
		out.HasTrial = model.B(*out.TrialKind != "none")
	}
	return out
}

func rowTrust(t model.TrustSignals) RowTrust {
	return RowTrust{Authenticity: t.ResidentialAuthenticity, KycVerified: t.KycVerified, ConsentDisclosed: t.ConsentDisclosed,
		StickinessAdvertised: t.StickinessAdvertised, StickinessHoldRate: t.StickinessHoldRate, BlacklistHitRate: t.BlacklistHitRate}
}

// trialKind reads site.pricing.trial's text (CMP-12): "none" (no trial),
// "sales" (a sales call or contact), "paid" (a price or a refund window),
// "card" (free, card required) or "free"; null without a trial text.
func trialKind(trial model.Str) model.Str {
	if trial == nil {
		return nil
	}
	t := strings.ToLower(strings.TrimSpace(*trial))
	switch {
	case t == "" || t == "none" || t == "no trial":
		return model.S("none")
	case strings.Contains(t, "sales") || strings.Contains(t, "contact"):
		return model.S("sales")
	case strings.ContainsAny(t, "$€£") || strings.Contains(t, "paid") || strings.Contains(t, "refund"):
		return model.S("paid")
	case strings.Contains(t, "card") && !strings.Contains(t, "no card"):
		return model.S("card")
	}
	return model.S("free")
}

// rowBrowser is a window's overall browser success score per engine; nil
// when the window has none.
func rowBrowser(w snap.Window) map[string]RowBrowserScore {
	all := browserOverall(w)
	if all == nil {
		return nil
	}
	out := make(map[string]RowBrowserScore, len(all))
	for engine, o := range all {
		out[engine] = RowBrowserScore{Success: o.Success.MetricScore}
	}
	return out
}

func targetCounts(targets []model.TargetMeta) TargetCounts {
	c := TargetCounts{All: len(targets)}
	for _, t := range targets {
		if t.Source == model.SourceMeasured {
			c.Measured++
		}
		if isCard(t) {
			c.Cards++
		}
	}
	return c
}

// isCard is true for a measured target outside any group: the ones a client
// shows as cards (not the AI source targets nor a catalog-only row).
func isCard(t model.TargetMeta) bool {
	return t.Source == model.SourceMeasured && t.Group == nil
}

// valueRanks is each network's place on its kind's cost board (costBoard
// over the sources of the same kind), by slug.
func valueRanks(srcs []rankingSource, rangeID string) map[string]int {
	byKind := map[string][]*entry{}
	stats := map[string]map[string]*WindowStats{}
	for _, s := range srcs {
		byKind[s.kind] = append(byKind[s.kind], s.e)
		stats[s.e.P.Slug] = s.stats
	}
	out := map[string]int{}
	for _, es := range byKind {
		for slug, rank := range costBoard(es, stats, rangeID).Ranks {
			out[slug] = rank
		}
	}
	return out
}

func rankingFleet(rows []RankingRow) RankingFleet {
	f := RankingFleet{Requests: 0, Successes: 0}
	for _, r := range rows {
		f.Requests, f.Successes = addNum(f.Requests, r.Requests), addNum(f.Successes, r.successes)
	}
	return f
}

// costSummary ports the true-cost page's summary over rows (in list order):
// priced networks by cost, cheapest first (ties keep the list order).
func costSummary(rows []RankingRow, cost func(RankingRow) Num) CostSummary {
	priced := []RankingRow{}
	for _, r := range rows {
		if cost(r).OK() {
			priced = append(priced, r)
		}
	}
	sort.SliceStable(priced, func(i, j int) bool { return cost(priced[i]) < cost(priced[j]) })
	out := CostSummary{Median: nan, SpreadX: nan, Priced: len(priced)}
	if len(priced) == 0 {
		return out
	}
	costs := make([]float64, len(priced))
	for i, r := range priced {
		costs[i] = cost(r).F()
	}
	first, last := priced[0], priced[len(priced)-1]
	out.Cheapest = &CostEnd{Slug: first.Slug, Name: first.Name, CostPer1kSuccess: cost(first)}
	out.Dearest = &CostEnd{Slug: last.Slug, Name: last.Name, CostPer1kSuccess: cost(last)}
	out.Median = Num(median(costs))
	if cost(first) > 0 {
		out.SpreadX = Num(cost(last).F() / cost(first).F())
	}
	return out
}
