package build

import (
	"encoding/json"
	"fmt"

	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/snap"
	"github.com/Zeus-Development/atlas/api/internal/store"
)

func ptrNum[T ~int16 | ~int32 | ~int64 | ~float64](v *T) Num {
	if v == nil {
		return nan
	}
	return Num(*v)
}

func decodeInto(raw json.RawMessage, v any, what string) error {
	if len(raw) == 0 {
		return nil
	}
	if err := json.Unmarshal(raw, v); err != nil {
		return fmt.Errorf("%s: %w", what, err)
	}
	return nil
}

func pricingFrom(r *store.PricingRow, plans []store.PlanRow) model.Pricing {
	p := model.Pricing{PricePerGb: nan, PricePerGbAtScale: nan, MinCommit: nan, CostPer1kSuccess: nan, AvgPayloadKb: nan,
		CostPer1kAllTargets: nan, HeadlinePriceRank: nan, ValueRank: nan, Plans: []model.PricingPlan{}, BasisPerGb: nan, BasisPlanGb: nan,
		TrialPrice: nan, TrialCredit: nan, TrialGb: nan, TrialDays: nan}
	if r == nil {
		return p
	}
	p.Model, p.Trial = r.Model, r.Trial
	p.TrialPrice, p.TrialCredit, p.TrialGb, p.TrialDays = ptrNum(r.TrialPrice), ptrNum(r.TrialCredit), ptrNum(r.TrialGb), ptrNum(r.TrialDays)
	p.TrialNeedsCard = r.TrialNeedsCard
	p.PricePerGb, p.PricePerGbAtScale, p.MinCommit = ptrNum(r.PricePerGb), ptrNum(r.PricePerGbAtScale), ptrNum(r.MinCommit)
	p.CostPer1kSuccess, p.AvgPayloadKb = ptrNum(r.CostPer1kSuccess), ptrNum(r.AvgPayloadKb)
	p.HeadlinePriceRank, p.ValueRank = ptrNum(r.HeadlinePriceRank), ptrNum(r.ValueRank)
	for _, pl := range plans {
		if pl.ProviderSlug != r.ProviderSlug || pl.Category != r.Category {
			continue
		}
		p.Plans = append(p.Plans, model.PricingPlan{Catalog: servedCatalog(pl.Catalog), Name: pl.Name, Audience: planAudience(pl.Catalog), Price: ptrNum(pl.Price), Gb: ptrNum(pl.Gb),
			Ips: ptrNum(pl.Ips), Recommended: pl.Recommended, DiscountPct: ptrNum(pl.DiscountPct)})
	}
	return p
}

// unservedCatalogKeys are a plan catalog's keys not served in its catalog:
// its English copy (API-PROSE-REMOVAL-1, #962: feature phrases and pool
// description), its size and period words (PRICING-CODES-1: the plan's gb and
// catalog.billing say them) and its audience (served as the plan's audience).
var unservedCatalogKeys = []string{"features", "poolDescription", "bandwidth", "period", "audience"}

// planAudience is a plan catalog's audience code; null without one.
func planAudience(raw json.RawMessage) model.Str {
	var c struct {
		Audience model.Str `json:"audience"`
	}
	if len(raw) == 0 || json.Unmarshal(raw, &c) != nil {
		return nil
	}
	return c.Audience
}

// servedCatalog is a plan's catalog without unservedCatalogKeys; one that is
// not a JSON object is served as it is.
func servedCatalog(raw json.RawMessage) json.RawMessage {
	var fields map[string]json.RawMessage
	if len(raw) == 0 || json.Unmarshal(raw, &fields) != nil || fields == nil {
		return raw
	}
	for _, k := range unservedCatalogKeys {
		delete(fields, k)
	}
	out, err := json.Marshal(fields)
	if err != nil {
		return raw
	}
	return out
}

func trustFrom(r *store.TrustRow) (model.TrustSignals, error) {
	t := model.TrustSignals{ResidentialAuthenticity: nan, DatacenterAsnShare: nan, ClaimGap: nan, BlacklistHitRate: nan,
		StickinessHoldRate: nan, LeakChecks: []model.LeakCheck{}}
	if r == nil {
		return t, nil
	}
	t.ResidentialAuthenticity, t.DatacenterAsnShare, t.ClaimGap = ptrNum(r.ResidentialAuthenticity), ptrNum(r.DatacenterAsnShare), ptrNum(r.ClaimGap)
	t.BlacklistHitRate, t.StickinessHoldRate = ptrNum(r.BlacklistHitRate), ptrNum(r.StickinessHoldRate)
	t.KycVerified, t.ConsentDisclosed = r.KycVerified, r.ConsentDisclosed
	if err := decodeInto(r.LeakChecks, &t.LeakChecks, "leak_checks"); err != nil {
		return t, err
	}
	if t.LeakChecks == nil {
		t.LeakChecks = []model.LeakCheck{}
	}
	return t, nil
}

type catalog struct {
	pricing map[[2]string]*store.PricingRow
	trust   map[[2]string]*store.TrustRow
	plans   []store.PlanRow
	prods   map[string][]store.ProductRow
}

func newCatalog(in *store.Inputs) *catalog {
	c := &catalog{pricing: map[[2]string]*store.PricingRow{}, trust: map[[2]string]*store.TrustRow{}, plans: in.Plans,
		prods: map[string][]store.ProductRow{}}
	for i := range in.Pricing {
		r := &in.Pricing[i]
		c.pricing[[2]string{r.ProviderSlug, r.Category}] = r
	}
	for i := range in.Trust {
		r := &in.Trust[i]
		c.trust[[2]string{r.ProviderSlug, r.Category}] = r
	}
	for _, r := range in.Products {
		c.prods[r.ProviderSlug] = append(c.prods[r.ProviderSlug], r)
	}
	return c
}

func (c *catalog) products(slug string) []model.ProviderProduct {
	out := []model.ProviderProduct{}
	for _, r := range c.prods[slug] {
		out = append(out, model.ProviderProduct{Kind: r.Kind, Measured: r.Measured, URL: r.URL})
	}
	return out
}

// mergeCatalog fills catalog pricing and trust into a measured block. Measured
// values win (the snapshot's list prices, wire-byte cost and authenticity);
// the cost basis is the snapshot's only (site.pricing has none);
// catalog values fill what the snapshot does not measure. w30 is the 30d
// snapshot metrics the cost per 1,000 successes and, without a catalog value,
// the average payload are derived from (nil without a snapshot product).
func mergeCatalog(slug, kind string, m *model.Measured, c *catalog, w30 *snap.Metrics) {
	pr := pricingFrom(c.pricing[[2]string{slug, kind}], c.plans)
	measured := m.Pricing
	if measured.PricePerGb.OK() || measured.PricePerGbAtScale.OK() {
		pr.PricePerGb, pr.PricePerGbAtScale = measured.PricePerGb, measured.PricePerGbAtScale
		if pr.Model == nil {
			pr.Model = measured.Model
		}
	}
	pr.BasisPerGb, pr.BasisPlanGb, pr.PriceBasis = measured.BasisPerGb, measured.BasisPlanGb, measured.PriceBasis
	if !pr.AvgPayloadKb.OK() && w30 != nil {
		pr.AvgPayloadKb = wireKbPerRequest(*w30)
	}
	if !pr.CostPer1kSuccess.OK() && w30 != nil {
		pr.CostPer1kSuccess = wireCostPer1k(costRate(pr), *w30)
	}
	if !pr.CostPer1kSuccess.OK() {
		pr.CostPer1kSuccess = costPer1k(costRate(pr), pr.AvgPayloadKb, m.Performance.SuccessRate)
	}
	m.Pricing = pr
	if r := c.trust[[2]string{slug, kind}]; r != nil && r.ClaimedPoolSize != nil {
		m.Pool.ClaimedPoolSize = Num(*r.ClaimedPoolSize)
	}
	if t, err := trustFrom(c.trust[[2]string{slug, kind}]); err == nil {
		if m.Trust.DatacenterAsnShare.OK() {
			t.DatacenterAsnShare = m.Trust.DatacenterAsnShare
		}
		if m.Trust.ClaimGap.OK() {
			t.ClaimGap = m.Trust.ClaimGap
		}
		if m.Trust.ResidentialAuthenticity.OK() {
			t.ResidentialAuthenticity = m.Trust.ResidentialAuthenticity
		}
		m.Trust = t
	}
}

// addHeadlinePriceRanks sets every network's pricing.headlinePriceRank the
// catalog gives none: its list rate's competition rank among the list rates of
// the networks of its kind, cheapest first, ties sharing the better rank (as
// valueRank ranks cost). A network's kinds are its product measurements, or
// its primary kind without one; its headline block takes its primary kind's.
func addHeadlinePriceRanks(entries []*entry) {
	byKind := map[string][]*model.Pricing{}
	for _, e := range entries {
		pm := e.P.ProductMeasurements
		if pm == nil || len(pm.Keys) == 0 {
			byKind[e.P.PrimaryKind] = append(byKind[e.P.PrimaryKind], &e.P.Measured.Pricing)
			continue
		}
		for _, k := range pm.Keys {
			byKind[k] = append(byKind[k], &pm.Get(k).Pricing)
		}
	}
	for _, prices := range byKind {
		var peers []Num
		for _, p := range prices {
			if r := displayRate(*p); r.OK() {
				peers = append(peers, r)
			}
		}
		for _, p := range prices {
			if !p.HeadlinePriceRank.OK() {
				p.HeadlinePriceRank = rankOf(displayRate(*p), peers, false).Rank
			}
		}
	}
	for _, e := range entries {
		if m := e.P.ProductMeasurements.Get(e.P.PrimaryKind); m != nil {
			e.P.Measured.Pricing.HeadlinePriceRank = m.Pricing.HeadlinePriceRank
		}
	}
}
