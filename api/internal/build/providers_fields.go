package build

import (
	"encoding/json"
	"fmt"

	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/Zeus-Development/proxy-index/api/internal/snap"
	"github.com/Zeus-Development/proxy-index/api/internal/store"
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
		HeadlinePriceRank: nan, ValueRank: nan, Plans: []model.PricingPlan{}}
	if r == nil {
		return p
	}
	p.Model, p.Trial, p.CostBasis = r.Model, r.Trial, r.CostBasis
	p.PricePerGb, p.PricePerGbAtScale, p.MinCommit = ptrNum(r.PricePerGb), ptrNum(r.PricePerGbAtScale), ptrNum(r.MinCommit)
	p.CostPer1kSuccess, p.AvgPayloadKb = ptrNum(r.CostPer1kSuccess), ptrNum(r.AvgPayloadKb)
	p.HeadlinePriceRank, p.ValueRank = ptrNum(r.HeadlinePriceRank), ptrNum(r.ValueRank)
	for _, pl := range plans {
		if pl.ProviderSlug != r.ProviderSlug || pl.Category != r.Category {
			continue
		}
		p.Plans = append(p.Plans, model.PricingPlan{Catalog: pl.Catalog, Name: pl.Name, Price: ptrNum(pl.Price), Gb: ptrNum(pl.Gb), Ips: ptrNum(pl.Ips),
			Note: pl.Note, Recommended: pl.Recommended, DiscountPct: ptrNum(pl.DiscountPct)})
	}
	return p
}

func trustFrom(r *store.TrustRow) (model.TrustSignals, error) {
	t := model.TrustSignals{ResidentialAuthenticity: nan, DatacenterAsnShare: nan, ClaimGap: nan, BlacklistHitRate: nan,
		StickinessHoldRate: nan, LeakChecks: []model.LeakCheck{}}
	if r == nil {
		return t, nil
	}
	t.ResidentialAuthenticity, t.DatacenterAsnShare, t.ClaimGap = ptrNum(r.ResidentialAuthenticity), ptrNum(r.DatacenterAsnShare), ptrNum(r.ClaimGap)
	t.BlacklistHitRate, t.StickinessHoldRate = ptrNum(r.BlacklistHitRate), ptrNum(r.StickinessHoldRate)
	t.StickinessAdvertised, t.KycVerified, t.ConsentDisclosed = r.StickinessAdvertised, r.KycVerified, r.ConsentDisclosed
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
		out = append(out, model.ProviderProduct{Kind: r.Kind, Name: r.Name, Measured: r.Measured, URL: r.URL, PriceLabel: r.PriceLabel, Note: r.Note})
	}
	return out
}

// labelledProducts is the catalog's products for slug. A catalog link must not
// discard a measured product's current list-rate label: a catalog product
// without a label takes the first measured product of its kind's.
func (c *catalog) labelledProducts(slug string, measured []model.ProviderProduct) []model.ProviderProduct {
	prods := c.products(slug)
	for i := range prods {
		if prods[i].PriceLabel != nil {
			continue
		}
		for _, m := range measured {
			if m.Kind == prods[i].Kind {
				prods[i].PriceLabel = m.PriceLabel
				break
			}
		}
	}
	return prods
}

func remove(list []string, v string) []string {
	out := list[:0]
	for _, x := range list {
		if x != v {
			out = append(out, x)
		}
	}
	return out
}

// mergeCatalog fills catalog pricing and trust into a measured block. Measured
// values win (the snapshot's list prices, wire-byte cost and authenticity);
// catalog values fill what the snapshot does not measure. w30 is the 30d
// snapshot metrics the cost per 1,000 successes is derived from (nil without
// a snapshot product).
func mergeCatalog(slug, kind string, m *model.Measured, c *catalog, w30 *snap.Metrics) {
	pr := pricingFrom(c.pricing[[2]string{slug, kind}], c.plans)
	measured := m.Pricing
	if measured.PricePerGb.OK() || measured.PricePerGbAtScale.OK() {
		pr.PricePerGb, pr.PricePerGbAtScale = measured.PricePerGb, measured.PricePerGbAtScale
		if pr.Model == nil {
			pr.Model = measured.Model
		}
	}
	if !pr.CostPer1kSuccess.OK() && w30 != nil {
		if cost := wireCostPer1k(listRate(pr), *w30); cost.OK() {
			pr.CostPer1kSuccess = cost
			if pr.CostBasis == nil {
				pr.CostBasis = model.S(costBasisText(listRate(pr), *w30))
			}
		}
	}
	if !pr.CostPer1kSuccess.OK() {
		if cost, basis := costPer1k(pr.PricePerGb, pr.AvgPayloadKb, m.Performance.SuccessRate); cost.OK() {
			pr.CostPer1kSuccess = cost
			if pr.CostBasis == nil {
				pr.CostBasis = model.S(basis)
			}
		}
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
