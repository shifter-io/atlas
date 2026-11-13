package build

import (
	"encoding/json"
	"math"
	"strings"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/Zeus-Development/proxy-index/api/internal/snap"
	"github.com/Zeus-Development/proxy-index/api/internal/store"
)

// COST-BASIS-200GB-1: every cost is priced at the basis (the first plan of
// 200 GB or more, else the largest plan); the displayed prices stay the list
// prices.

// setPricing gives provider's snapshot products list prices and a cost basis
// (a 250 GB first_200gb_plus plan; NaN: null), as an every-request snapshot
// publishes them: the catalog's site.pricing has no basis.
func setPricing(doc map[string]any, provider string, entry, lowest, basis float64) {
	orNil := func(v float64) any {
		if math.IsNaN(v) {
			return nil
		}
		return v
	}
	for _, p := range doc["products"].([]any) {
		if p := p.(map[string]any); p["provider"] == provider {
			p["pricing"] = map[string]any{"currency": "USD", "entry_per_gb": orNil(entry), "lowest_per_gb": orNil(lowest),
				"basis_per_gb": orNil(basis), "basis_plan_gb": 250, "price_basis": "first_200gb_plus"}
		}
	}
}

func basisPricing(display, basis Num, kind string) model.Pricing {
	p := model.Pricing{PricePerGb: nan, PricePerGbAtScale: display, BasisPerGb: basis, BasisPlanGb: nan, HeadlinePriceRank: nan}
	if kind != "" {
		p.PriceBasis, p.BasisPlanGb = model.S(kind), 200
	}
	return p
}

func TestCostRateIsTheBasisWithNoFallback(t *testing.T) {
	t.Parallel()
	p := model.Pricing{PricePerGb: 3, PricePerGbAtScale: 2, BasisPerGb: nan}
	if costRate(p).OK() || displayRate(p) != 2 {
		t.Errorf("without a basis: cost rate %v (want null), display %v", costRate(p), displayRate(p))
	}
	p.BasisPerGb = 2.5
	if costRate(p) != 2.5 || displayRate(p) != 2 {
		t.Errorf("cost rate %v, display %v", costRate(p), displayRate(p))
	}
}

// The snapshot's basis reaches the measured pricing and survives the
// catalog merge; an older snapshot without it decodes to null.
func TestMeasuredPricingCarriesTheBasisThroughTheCatalog(t *testing.T) {
	t.Parallel()
	var old snap.Pricing
	if err := json.Unmarshal([]byte(`{"lowest_per_gb": 1, "entry_per_gb": 2}`), &old); err != nil ||
		old.BasisPerGb.OK() || old.BasisPlanGb.OK() || old.PriceBasis != nil {
		t.Fatalf("older snapshot pricing %+v (%v)", old, err)
	}
	sp := &snap.Product{Pricing: &snap.Pricing{LowestPerGb: 0.75, EntryPerGb: 1, BasisPerGb: 2.1, BasisPlanGb: 250,
		PriceBasis: model.S("first_200gb_plus")}}
	w30 := snap.Metrics{Completed: 1000, BytesUp: 48_000, BytesDown: 2_000_000, RequestSuccessRate: 0.5, SuccessRate: 0.5}
	m := model.EmptyMeasured()
	m.Pricing = measuredPricing(sp, w30)
	if m.Pricing.PricePerGbAtScale != 0.75 || m.Pricing.BasisPerGb != 2.1 || !near(m.Pricing.CostPer1kSuccess, wireCostPer1k(2.1, w30).F()) {
		t.Errorf("measured pricing %+v", m.Pricing)
	}
	price := 5.0
	cat := &catalog{pricing: map[[2]string]*store.PricingRow{{"a", "residential"}: {ProviderSlug: "a", Category: "residential", PricePerGb: &price}},
		trust: map[[2]string]*store.TrustRow{}}
	mergeCatalog("a", "residential", &m, cat, &w30)
	pr := m.Pricing
	if pr.BasisPerGb != 2.1 || pr.BasisPlanGb != 250 || model.SV(pr.PriceBasis) != "first_200gb_plus" || pr.PricePerGb != 1 ||
		!near(pr.CostPer1kSuccess, wireCostPer1k(2.1, w30).F()) {
		t.Errorf("merged pricing %+v", pr)
	}
	// A catalog price alone has no basis: no cost.
	m = model.EmptyMeasured()
	mergeCatalog("a", "residential", &m, cat, &w30)
	if m.Pricing.PricePerGb != 5 || costRate(m.Pricing).OK() || m.Pricing.CostPer1kSuccess.OK() || m.Pricing.PriceBasis != nil {
		t.Errorf("catalog-only pricing %+v", m.Pricing)
	}
}

// listRate is served as the basis, with priceBasis and basisPlanGb, on the
// rankings rows and the network page's pricing; the list prices and their
// label stay as they are, and no separate basis price is served.
func TestListRateIsTheBasisWithItsPlan(t *testing.T) {
	t.Parallel()
	p := basisPricing(1.5, 2.85, "first_200gb_plus")
	p.PricePerGb = 4
	row := rowPricing(p)
	if row.ListRate != 2.85 || model.SV(row.PriceBasis) != "first_200gb_plus" || row.BasisPlanGb != 200 ||
		row.PricePerGbAtScale != 1.5 || row.PricePerGb != 4 {
		t.Errorf("row pricing %+v", row)
	}
	body, err := json.Marshal(profilePricing(p))
	if err != nil {
		t.Fatal(err)
	}
	var served map[string]any
	if err := json.Unmarshal(body, &served); err != nil {
		t.Fatal(err)
	}
	if served["listRate"] != 2.85 || served["priceBasis"] != "first_200gb_plus" || served["basisPlanGb"] != 200.0 ||
		served["pricePerGbAtScale"] != 1.5 || strings.Contains(string(body), "basisPerGb") {
		t.Errorf("profile pricing %s", body)
	}
	if r := rowPricing(basisPricing(1.5, nan, "")); r.ListRate.OK() || r.PriceBasis != nil || r.BasisPlanGb.OK() || r.PricePerGbAtScale != 1.5 {
		t.Errorf("no basis: %+v", r)
	}
}

// The headline price rank and the market's cheapest and dearest stay on the
// displayed list rate, whatever the basis.
func TestDisplayFiguresStayOnTheListRate(t *testing.T) {
	t.Parallel()
	entries := []*entry{}
	stats := map[string]map[string]*WindowStats{}
	// a is cheaper by list rate, b by basis.
	for _, c := range []struct {
		slug           string
		display, basis Num
	}{{"a", 1, 3}, {"b", 2, 0.5}} {
		p := &model.Provider{Slug: c.slug, Name: c.slug, PrimaryKind: "residential", Measured: model.EmptyMeasured()}
		p.Pricing = basisPricing(c.display, c.basis, "first_200gb_plus")
		entries = append(entries, &entry{P: p})
		stats[c.slug] = map[string]*WindowStats{"30d": {LatencyP50: nan, SuccessRate: nan}}
	}
	addHeadlinePriceRanks(entries)
	if a, b := entries[0].P.Measured.Pricing.HeadlinePriceRank, entries[1].P.Measured.Pricing.HeadlinePriceRank; a != 1 || b != 2 {
		t.Errorf("headline price ranks %v %v, want 1 2 by list rate", a, b)
	}
	v := &FleetView{Range: RangeMeta{ID: "30d"}}
	pickLeaders(v, entries, stats)
	if v.Cheapest.Cost != 1 || v.Dearest.Cost != 2 || v.Cheapest.Provider.Slug != "a" {
		t.Errorf("cheapest %+v dearest %+v, want the list rates", v.Cheapest, v.Dearest)
	}
}

// insights.valueAll rows serve the plan their costs are priced at, null
// with the catalog reason without one.
func TestValueAllRowsServeTheirBasis(t *testing.T) {
	t.Parallel()
	priced, unpriced := vaEntry("p", 2, 1, nil), vaEntry("u", nan.F(), 1, nil)
	pm := priced.P.ProductMeasurements.Get(valueAllKind)
	pm.Pricing.PriceBasis, pm.Pricing.BasisPlanGb = model.S("largest_below_200gb"), 100
	v := homeValueAll(vaContext(priced, unpriced), vaRows("p", "u"), "30d")
	p, u := v.Rows[0], v.Rows[1]
	if p.Slug != "p" || model.SV(p.PriceBasis) != "largest_below_200gb" || p.BasisPlanGb != 100 || !near(p.CostPer1k, 1) {
		t.Errorf("priced row %+v", p)
	}
	if u.PriceBasis != nil || u.BasisPlanGb.OK() || u.ReasonOf("priceBasis") != NACatalog || u.ReasonOf("basisPlanGb") != NACatalog {
		t.Errorf("unpriced row %+v", u)
	}
}
