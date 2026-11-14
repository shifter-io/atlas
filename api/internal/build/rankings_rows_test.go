package build

import (
	"encoding/json"
	"math"
	"testing"

	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/store"
	"github.com/Zeus-Development/atlas/api/internal/testscale"
)

// rowsPages builds a synthetic build with site prices for net00 and net01
// (of net01Kind; net02 unpriced), their snapshot's list prices and cost
// basis the same, after edit has changed its published snapshot.
func rowsPages(t *testing.T, net01Kind string, edit func(doc map[string]any)) *Pages {
	t.Helper()
	in, err := testscale.Inputs(testscale.Shape{Networks: 3, Countries: 1, Exits: 3, Tail: 5, Unmeasured: 1, Browser: true})
	if err != nil {
		t.Fatal(err)
	}
	in.Snapshot = editSnapshot(t, in.Snapshot, func(doc map[string]any) {
		if edit != nil {
			edit(doc)
		}
		setPricing(doc, "net00", 4, 3, 3)
		setPricing(doc, "net01", 1, math.NaN(), 1)
	})
	f := func(v float64) *float64 { return &v }
	s := func(v string) *string { return &v }
	in.Pricing = append(in.Pricing,
		store.PricingRow{ProviderSlug: "net00", Category: "residential", PricePerGb: f(4), PricePerGbAtScale: f(3), Trial: s("1 GB free, no card")},
		store.PricingRow{ProviderSlug: "net01", Category: net01Kind, PricePerGb: f(1), Trial: s("Sales call required")})
	res, err := Build(in)
	if err != nil {
		t.Fatal(err)
	}
	return res.Pages
}

func rowOf(t *testing.T, ans *RankingsAnswer, slug string) RankingRow {
	t.Helper()
	for _, r := range ans.Networks {
		if r.Slug == slug {
			return r
		}
	}
	t.Fatalf("%s is not in the %s ranking", slug, ans.Range)
	return RankingRow{}
}

// setKind moves a network's product groups to another proxy kind.
func setKind(doc map[string]any, provider, kind string) {
	for _, p := range doc["products"].([]any) {
		if p := p.(map[string]any); p["provider"] == provider {
			p["kind"], p["product"] = kind, model.DefaultProduct[kind]
			p["criteria"].(map[string]any)["kind"] = kind
		}
	}
}

// setAlone leaves a network's balanced score of every window unranked, with
// itself as the only ready network of its kind.
func setAlone(doc map[string]any, provider string) {
	for _, p := range doc["products"].([]any) {
		if p := p.(map[string]any); p["provider"] == provider {
			for _, w := range p["windows"].(map[string]any) {
				s := w.(map[string]any)["scores"].(map[string]any)["balanced"].(map[string]any)
				s["score"], s["grade"], s["rank"] = 80.0, "B", nil
				for _, c := range s["components"].(map[string]any) {
					c.(map[string]any)["inputs"] = map[string]any{"peers": 1}
				}
			}
		}
	}
}

func TestRankingRowsCarryPricesTrialTrustAndTheCostSummary(t *testing.T) {
	t.Parallel()
	pg := rowsPages(t, "residential", nil)
	ans := pg.Rankings["30d"]
	a, b, c := rowOf(t, ans, "net00"), rowOf(t, ans, "net01"), rowOf(t, ans, "net02")
	if a.ListRate != 3 || a.PricePerGb != 4 || a.PricePerGbAtScale != 3 ||
		model.SV(a.TrialKind) != "free" || a.HasTrial == nil || !*a.HasTrial {
		t.Errorf("net00 pricing %+v", a.RowPricing)
	}
	if b.ListRate != 1 || b.PricePerGbAtScale.OK() || model.SV(b.TrialKind) != "sales" || !*b.HasTrial {
		t.Errorf("net01 pricing %+v", b.RowPricing)
	}
	if c.ListRate.OK() || c.HasTrial != nil || c.TrialKind != nil || c.CostPer1k.OK() {
		t.Errorf("unpriced net02 %+v", c.RowPricing)
	}
	if !a.CostPer1k.OK() || !b.CostPer1k.OK() || b.CostPer1k >= a.CostPer1k {
		t.Fatalf("costs %v %v", a.CostPer1k, b.CostPer1k)
	}
	// The unpriced network is left out of the summary; the cheapest is first.
	cost := costSummary(ans.Networks, httpCost)
	if cost.Priced != 2 || cost.Cheapest.Slug != "net01" || cost.Dearest.Slug != "net00" || cost.Cheapest.CostPer1kSuccess != b.CostPer1k ||
		cost.Median != (a.CostPer1k+b.CostPer1k)/2 || cost.SpreadX != a.CostPer1k/b.CostPer1k {
		t.Errorf("cost %+v cheapest %+v dearest %+v", cost, cost.Cheapest, cost.Dearest)
	}
	// The answer serves the all-targets summary only (HTTP-COST-RETIRE-1).
	var served map[string]any
	if body, err := json.Marshal(ans.Cost); err != nil || json.Unmarshal(body, &served) != nil || len(served) != 1 || served["allTargets"] == nil {
		t.Errorf("cost %s (%v)", body, err)
	}
	if *b.ValueRankAllTargets != 1 || *a.ValueRankAllTargets != 2 || c.ValueRankAllTargets != nil {
		t.Errorf("value ranks %v %v %v", b.ValueRankAllTargets, a.ValueRankAllTargets, c.ValueRankAllTargets)
	}
	// Every listed network's requests, those without a value left out.
	want := Num(0)
	for _, r := range ans.Networks {
		want = addNum(want, r.Requests)
	}
	if want < a.Requests+b.Requests+c.Requests || ans.Fleet.Requests != want || !ans.Fleet.Successes.OK() || ans.Fleet.Successes > want {
		t.Errorf("fleet %+v, want %v requests", ans.Fleet, want)
	}
	for _, r := range []RankingRow{a, b, c} {
		if !r.LatencyP90.OK() || !r.LatencyP99.OK() || !r.Requests.OK() || !r.Cities.OK() || !r.Asns.OK() || !r.ExclusiveShare.OK() ||
			len(r.TrendT) != len(r.Trend) || len(r.Trend) == 0 || r.BrowserOverall == nil || !r.Standing.Rank.OK() {
			t.Errorf("%s figures %+v", r.Slug, r)
		}
	}
	last := ans.Networks[len(ans.Networks)-1]
	if last.Slug != "net03" || last.BrowserOverall != nil || last.Standing.Rank.OK() || last.Cities.OK() || len(last.TrendT) != len(last.Trend) {
		t.Errorf("unmeasured %+v", last)
	}
	if ans.Targets.All == 0 || ans.Targets.Measured == 0 || ans.Targets.Cards == 0 || ans.Targets.Cards >= ans.Targets.All || ans.Profiles == nil {
		t.Errorf("targets %+v profiles %v", ans.Targets, ans.Profiles)
	}
}

func TestTheKindFilterRanksWithinTheKind(t *testing.T) {
	t.Parallel()
	pg := rowsPages(t, "datacenter", func(doc map[string]any) {
		setKind(doc, "net01", "datacenter")
		setAlone(doc, "net01")
	})
	if len(pg.RankingsByKind) != len(Ranges)*len(model.ProxyKinds) {
		t.Fatalf("%d kind answers", len(pg.RankingsByKind))
	}
	for _, r := range Ranges {
		dc, res := pg.RankingsByKind[r.ID+"/datacenter"], pg.RankingsByKind[r.ID+"/residential"]
		if len(dc.Networks) != 1 || dc.Networks[0].Slug != "net01" || dc.Networks[0].Position != 1 {
			t.Fatalf("%s datacenter %+v", r.ID, dc.Networks)
		}
		// Alone in its kind with a score: unranked by the framework, #1 of 1.
		lone := dc.Networks[0]
		if lone.Rank.OK() || lone.Standing.Rank != 1 || lone.Standing.Peers != 1 || lone.Kind != "datacenter" || *lone.ValueRankAllTargets != 1 {
			t.Errorf("%s lone %+v", r.ID, lone)
		}
		if c := costSummary(dc.Networks, httpCost); c.Priced != 1 || c.Cheapest.Slug != "net01" || c.SpreadX != 1 || dc.Fleet.Requests != lone.Requests {
			t.Errorf("%s datacenter cost %+v fleet %+v", r.ID, c, dc.Fleet)
		}
		for i, row := range res.Networks {
			if row.Slug == "net01" || row.Position != i+1 || row.Kind != "residential" {
				t.Errorf("%s residential row %+v", r.ID, row)
			}
		}
		// net00 is the only priced residential network now: first on value.
		if v := rowOf(t, res, "net00").ValueRankAllTargets; v == nil || *v != 1 {
			t.Errorf("%s residential value rank %v", r.ID, v)
		}
		m := pg.RankingsByKind[r.ID+"/mobile"]
		if c := costSummary(m.Networks, httpCost); len(m.Networks) != 0 || c.Priced != 0 || c.Cheapest != nil || m.Cost.AllTargets.Priced != 0 || m.Fleet.Requests != 0 {
			t.Errorf("%s mobile %+v", r.ID, m)
		}
	}
	// Without kind, every network is listed and ranks on value within its own kind.
	all := pg.Rankings["30d"]
	if len(all.Networks) != 4 || *rowOf(t, all, "net01").ValueRankAllTargets != 1 || *rowOf(t, all, "net00").ValueRankAllTargets != 1 {
		t.Errorf("every network %+v", all.Networks)
	}
}

func TestTrialKindReadsTheTrialText(t *testing.T) {
	t.Parallel()
	for text, want := range map[string]string{"None": "none", " none ": "none", "": "none", "Sales call required": "sales",
		"Contact us": "sales", "$3 / 100 MB, card required": "paid", "$1 / 5 IPs / 3 days": "paid", "3-day refund window": "paid",
		"Free trial, card required": "card", "500 MB free, no card": "free", "1 GB free, no card": "free",
		"$5 welcome credit (one-time, on registration)": "credit"} {
		if got := model.SV(trialKind(model.S(text))); got != want {
			t.Errorf("%q: %q, want %q", text, got, want)
		}
	}
	if trialKind(nil) != nil {
		t.Error("no trial text has a kind")
	}
	if p := rowPricing(model.Pricing{PricePerGb: nan, PricePerGbAtScale: nan, Trial: model.S("None")}); p.HasTrial == nil || *p.HasTrial {
		t.Errorf("a trial of none %+v", p)
	}
}

// httpCost is a row's plain-HTTP cost, the input costSummary is tested over
// (the answer serves only the all-targets summary).
func httpCost(r RankingRow) Num { return r.CostPer1k }
