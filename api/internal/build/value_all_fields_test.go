package build

import (
	"math"
	"strings"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/Zeus-Development/proxy-index/api/internal/snap"
)

// billWindow is a window whose card targets each have 1,000 tries and
// bytes[i] wire bytes (successes[i] successes).
func billWindow(cards []string, bytes, successes []float64) snap.Window {
	w := snap.Window{Targets: map[string]snap.TargetResult{}}
	for i, id := range cards {
		w.Targets[id] = snap.TargetResult{Metrics: snap.Metrics{Completed: 1000, Successes: Num(successes[i]),
			BytesUp: 0, BytesDown: Num(bytes[i]), RequestSuccessRate: Num(successes[i] / 1000)}}
	}
	return w
}

// billProduct is a product whose 30d window is w.
func billProduct(w snap.Window) *snap.Product {
	return &snap.Product{Windows: map[string]snap.Window{"30d": w}}
}

func TestAllTargetsBillIsTheCardTargetsMean(t *testing.T) {
	t.Parallel()
	cards := []string{"a", "b"}
	// $2/GB: 2e9 bytes over 1,000 successes is $4 per 1k, 1e9 bytes $2.
	w := billWindow(cards, []float64{2e9, 1e9}, []float64{1000, 1000})
	if b := billOver(2, w, cards); b.bill != 3 || b.counted != 2 {
		t.Errorf("bill %v over %d, want (4 + 2) / 2 = 3 over 2", b.bill, b.counted)
	}
	w = billWindow(cards, []float64{2e9, 1e9}, []float64{1000, 0})
	if b := billOver(2, w, cards); b.bill.OK() || b.counted != 1 {
		t.Errorf("a target without successes: bill %v over %d, want null over 1", b.bill, b.counted)
	}
	if b := billOver(nan, billWindow(cards, []float64{2e9, 1e9}, []float64{1000, 1000}), cards); b.bill.OK() || b.counted != 0 {
		t.Errorf("no price: bill %v over %d, want null over 0", b.bill, b.counted)
	}
	if b := billOver(2, snap.Window{}, nil); b.bill.OK() {
		t.Errorf("no card target: bill %v, want null", b.bill)
	}
}

func TestExistingCostsAreUnroundedInDecimalGB(t *testing.T) {
	t.Parallel()
	// 83 bytes per request at $2/GB, all successful: 0.000166 per 1,000, not 0.0002.
	m := snap.Metrics{BytesDown: 83_000, Completed: 1000, Successes: 1000, RequestSuccessRate: 1, SuccessRate: 1}
	if got := wireCostPer1k(2, m); math.Abs(got.F()-0.000166) > 1e-15 {
		t.Errorf("cost %v, want 0.000166 unrounded", got)
	}
	if got := wireGbPer1k(snap.Metrics{BytesDown: 1e9, Completed: 1000}); got != 1 {
		t.Errorf("1e9 bytes over 1,000 requests: %v GB per 1k, want 1", got)
	}
	if got := wireKbPerRequest(snap.Metrics{BytesDown: 2_048_000, Completed: 1000}); got != 2 {
		t.Errorf("avgPayloadKb %v, want 2 (KB = 1,024 bytes)", got)
	}
}

// A /v1/targets cell's cost is valueAll's byTarget cost (acceptance 1).
func TestCellCostMatchesTheTargetCost(t *testing.T) {
	t.Parallel()
	for _, c := range []struct{ bytes, completed, successes, price float64 }{
		{1_437_000, 2388378, 1781113, 3.5}, {451_000_000, 100, 85, 2.75}, {335_280_000, 100, 40, 4.1},
	} {
		m := snap.Metrics{BytesDown: Num(c.bytes), Completed: Num(c.completed), Successes: Num(c.successes),
			RequestSuccessRate: Num(c.successes / c.completed)}
		tc, why := targetCost(Num(c.price), m)
		cell := wireCostPer1k(Num(c.price), m)
		if why != "" || math.Abs(cell.F()/tc.CostPer1k.F()-1) > 1e-12 {
			t.Errorf("%+v: cell %v, target cost %v (%s)", c, cell, tc.CostPer1k, why)
		}
	}
}

func TestRankAllTargetsSharesTiesAndSkipsMissingBills(t *testing.T) {
	t.Parallel()
	row := func(slug, kind string, bill float64) RankingRow {
		r := RankingRow{Slug: slug, Kind: kind}
		r.CostPer1kAllTargets = Num(bill)
		return r
	}
	rows := []RankingRow{row("dear", "residential", 3), row("tie1", "residential", 1), row("incomplete", "residential", nan.F()),
		row("tie2", "residential", 1), row("dc", "datacenter", 9)}
	rankAllTargets(rows)
	want := map[string]int{"dear": 3, "tie1": 1, "tie2": 1, "dc": 1}
	for _, r := range rows {
		w, ok := want[r.Slug]
		if !ok {
			if r.ValueRankAllTargets != nil {
				t.Errorf("%s: rank %d, want null", r.Slug, *r.ValueRankAllTargets)
			}
			continue
		}
		if r.ValueRankAllTargets == nil || *r.ValueRankAllTargets != w {
			t.Errorf("%s: rank %v, want %d", r.Slug, r.ValueRankAllTargets, w)
		}
	}
	sum := costSummary(rows, func(r RankingRow) Num { return r.CostPer1kAllTargets })
	if sum.Priced != 4 || sum.Cheapest.Slug != "tie1" || sum.Dearest.Slug != "dc" || sum.Median != 2 || sum.SpreadX != 9 {
		t.Errorf("all-targets summary %+v", sum)
	}
}

func TestHomeSpreadAllIsValueAllsSpread(t *testing.T) {
	t.Parallel()
	if s := homeSpreadAll(HomeValueAll{SpreadX: nan}); s != nil {
		t.Errorf("no complete row: %+v, want null", s)
	}
	v := HomeValueAll{Best: &ValueAllPick{Slug: "a", Name: "A", CostPer1k: 1}, Worst: &ValueAllPick{Slug: "b", Name: "B", CostPer1k: 3}, SpreadX: 3}
	if s := homeSpreadAll(v); s == nil || s.Value != 3 || *s.Best != (HomeRef{"a", "A"}) || *s.Worst != (HomeRef{"b", "B"}) {
		t.Errorf("spreadAll %+v", s)
	}
}

func TestPricingAllTargetsAndItsBasis(t *testing.T) {
	t.Parallel()
	cards := []string{"a", "b"}
	pr := model.Pricing{PricePerGb: 3, PricePerGbAtScale: 2}
	fillPricingAllTargets(&pr, billProduct(billWindow(cards, []float64{2e9, 1e9}, []float64{1000, 0})), cards)
	if pr.CostPer1kAllTargets.OK() || !strings.Contains(model.SV(pr.CostBasisAllTargets), "at $2.00/GB") ||
		!strings.HasSuffix(model.SV(pr.CostBasisAllTargets), "; 1 of 2 targets measured.") {
		t.Errorf("incomplete: %v, %q", pr.CostPer1kAllTargets, model.SV(pr.CostBasisAllTargets))
	}
	pr = model.Pricing{PricePerGb: nan, PricePerGbAtScale: nan}
	fillPricingAllTargets(&pr, billProduct(billWindow(cards, []float64{2e9, 1e9}, []float64{1000, 1000})), cards)
	if pr.CostPer1kAllTargets.OK() || pr.CostBasisAllTargets != nil {
		t.Errorf("unpriced: %v, %v", pr.CostPer1kAllTargets, pr.CostBasisAllTargets)
	}
}

func TestAllTargetsNullReasons(t *testing.T) {
	t.Parallel()
	priced, unpriced := naFacts{measured: true, ran: true, priced: true}, naFacts{measured: true, ran: true}
	for _, c := range []struct {
		f    naFacts
		path string
		want string
	}{
		{priced, "costPer1kAllTargets", NAIncompleteTargets}, {priced, "valueRankAllTargets", NAIncompleteTargets},
		{priced, "market.spreadAll", NAIncompleteTargets}, {priced, "cost.allTargets.cheapest", NAIncompleteTargets},
		{priced, "cost.cheapest", NAInsufficient}, {unpriced, "costPer1kAllTargets", NACatalog},
		{unpriced, "cost.allTargets.median", NACatalog}, {naFacts{priced: true}, "pricing.costPer1kAllTargets", NAIncompleteTargets},
		{priced, "pricing.costBasisAllTargets", NACatalog},
	} {
		if got := c.f.code(c.path); got != c.want {
			t.Errorf("%s: %s, want %s", c.path, got, c.want)
		}
	}
}
