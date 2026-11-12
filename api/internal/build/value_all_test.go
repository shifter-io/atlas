package build

import (
	"encoding/json"
	"math"
	"slices"
	"strings"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/Zeus-Development/proxy-index/api/internal/snap"
)

// vaCards are the card targets of vaContext, in dataset order.
var vaCards = []string{"generic_http", "generic_https", "amazon_pdp", "google_search", "linkedin_public", "instagram_web",
	"cloudflare_challenge", "ai_answers"}

func vaMetrics(tries, successes, bytes float64) snap.Metrics {
	return snap.Metrics{Completed: Num(tries), Successes: Num(successes), BytesUp: Num(bytes / 4), BytesDown: Num(bytes * 3 / 4)}
}

// vaEntry is a residential network at price $/GB (NaN: unpriced) with the
// 30d target metrics given; each card target without one gets 100 tries,
// 50 successes and cost × 50 GB of bytes (a cost of cost × 1000 per 1,000).
func vaEntry(slug string, price, cost float64, targets map[string]snap.Metrics) *entry {
	w := snap.EmptyWindow()
	w.Targets = map[string]snap.TargetResult{}
	perGb := price
	if math.IsNaN(perGb) {
		perGb = 1
	}
	for _, id := range vaCards {
		m, ok := targets[id]
		if !ok {
			m = vaMetrics(100, 50, cost*50e9/perGb/1000)
		}
		w.Targets[id] = snap.TargetResult{Metrics: m}
	}
	pm := model.NewMeasurementMap()
	pm.Set(valueAllKind, &model.ProductMeasurement{Kind: valueAllKind,
		Measured: model.Measured{Pricing: model.Pricing{PricePerGb: Num(price), PricePerGbAtScale: nan}}})
	return &entry{P: &model.Provider{Slug: slug, Name: "Net " + slug, ProductMeasurements: pm},
		Snap: map[string]*snap.Product{valueAllKind: {Windows: map[string]snap.Window{"30d": w}}}}
}

// vaContext has the 8 card targets plus a group's source and a catalog
// target, which are not cards.
func vaContext(entries ...*entry) *Context {
	group := "ai"
	targets := []model.TargetMeta{{ID: "queue", Source: "catalog"}}
	for _, id := range vaCards {
		targets = append(targets, model.TargetMeta{ID: id, Source: model.SourceMeasured})
		if id == "google_search" {
			targets = append(targets, model.TargetMeta{ID: "ai_source", Source: model.SourceMeasured, Group: &group})
		}
	}
	return &Context{Entries: entries, Dataset: &model.Dataset{Targets: targets}}
}

func vaRows(slugs ...string) []RankingRow {
	out := []RankingRow{}
	for _, s := range slugs {
		out = append(out, RankingRow{Slug: s, Name: "Net " + s, Kind: valueAllKind, Source: model.SourceMeasured})
	}
	return out
}

func vaBySlug(v HomeValueAll) map[string]ValueAllRow {
	out := map[string]ValueAllRow{}
	for _, r := range v.Rows {
		out[r.Slug] = r
	}
	return out
}

func vaNear(a, b Num) bool { return math.Abs(a.F()-b.F()) <= 1e-12*math.Abs(b.F()) }

// One target's cost: price × bytes ÷ 10^9 ÷ successes × 1000, unrounded.
func TestTargetCostIsUnrounded(t *testing.T) {
	t.Parallel()
	tc, why := targetCost(2, vaMetrics(1200, 1000, 2e9))
	if why != "" || tc.CostPer1k != 4 || tc.Tries != 1200 || tc.Successes != 1000 || tc.MbPerTry != Num(2e9/1200/1e6) {
		t.Errorf("4.0 per 1,000: %+v %q", tc, why)
	}
	if tc, _ := targetCost(1, vaMetrics(6000, 6000, 1e9)); tc.CostPer1k != Num(1.0/6) {
		t.Errorf("a sixth stays a sixth: %v", tc.CostPer1k)
	}
}

// For a window whose request success rate is successes ÷ completed, the
// cost is the cell formula (wireCostPer1k's inputs) at 10^9 bytes per GB.
func TestTargetCostIsTheCellFormulaAtDecimalGB(t *testing.T) {
	t.Parallel()
	m := vaMetrics(2388378, 1781113, 1433026800)
	m.RequestSuccessRate = m.Successes / m.Completed
	price := Num(4.5)
	want := wireGbPer1k(m) * 1073741824 / gbBytes * price / m.RequestSuccessRate
	if tc, _ := targetCost(price, m); !vaNear(tc.CostPer1k, want) {
		t.Errorf("cost %v, want %v", tc.CostPer1k, want)
	}
}

// Each null cost has its reason, the first that applies; tries, successes
// and MB per try are filled whenever the data exists.
func TestTargetCostNullReasons(t *testing.T) {
	t.Parallel()
	missing := vaMetrics(10, 5, 0)
	missing.BytesUp, missing.BytesDown = nan, nan
	for _, c := range []struct {
		name      string
		price     Num
		m         snap.Metrics
		why       string
		successes int
		mb        bool
	}{
		{"no entry", 2, snap.Metrics{}, NANoRun, 0, false},
		{"no tries", 2, vaMetrics(0, 0, 0), NANoRun, 0, false},
		{"missing counts", 2, snap.Metrics{Completed: nan, Successes: nan, BytesDown: 5}, NANoRun, 0, false},
		{"no successes", 2, vaMetrics(10, 0, 1e6), NANoSuccesses, 0, true},
		{"no successes, unpriced", nan, vaMetrics(10, 0, 1e6), NANoSuccesses, 0, true},
		{"bytes missing", 2, missing, NANotMeasured, 5, false},
		{"bytes 0", 2, vaMetrics(10, 5, 0), NANotMeasured, 5, false},
		{"no price", nan, vaMetrics(10, 5, 1e6), NACatalog, 5, true},
	} {
		tc, why := targetCost(c.price, c.m)
		if why != c.why || tc.CostPer1k.OK() || tc.Successes != c.successes || tc.MbPerTry.OK() != c.mb {
			t.Errorf("%s: %+v %q, want %q", c.name, tc, why, c.why)
		}
	}
}

// The bill is the plain mean of the 8 card targets' costs; a row missing a
// target has only a partial bill and sorts after every complete row; an
// unpriced row sorts last. Below 20 successes a counted target is low-sample.
func TestValueAllBill(t *testing.T) {
	t.Parallel()
	gap := map[string]snap.Metrics{"google_search": vaMetrics(100, 0, 1e8)}
	low := map[string]snap.Metrics{"amazon_pdp": vaMetrics(100, 19, 19e6), "linkedin_public": vaMetrics(100, 20, 20e6)}
	mixed := map[string]snap.Metrics{"generic_http": vaMetrics(1000, 1000, 1e9), "generic_https": vaMetrics(1000, 500, 3e9)}
	c := vaContext(vaEntry("nopr", nan.F(), 1, nil), vaEntry("gap", 1, 0.5, gap), vaEntry("dear", 3, 6, nil),
		vaEntry("mixed", 1, 2, mixed), vaEntry("low", 1, 1, low))
	v := homeValueAll(c, vaRows("nopr", "gap", "dear", "mixed", "low"), "30d")
	if !slices.Equal(v.Targets, vaCards) || v.Unit != "usd_per_1k_successes" || v.GbBytes != 1e9 || v.Weighting != "equal" {
		t.Fatalf("header %+v", v)
	}
	if got := slugsOf(v.Rows, func(r ValueAllRow) string { return r.Slug }); !sameSlugs(got, "low", "mixed", "dear", "gap", "nopr") {
		t.Fatalf("order %v", got)
	}
	rows := vaBySlug(v)
	// mixed: 1.0 and 6.0 on the HTTP targets, 2.0 on the 6 others.
	if m := rows["mixed"]; !vaNear(m.CostPer1k, Num((1+6+6*2)/8.0)) || m.PartialCostPer1k.OK() || m.TargetsCounted != 8 ||
		len(m.MissingTargets) != 0 || len(m.NA) != 0 {
		t.Errorf("mixed %+v", m)
	}
	if l := rows["low"]; !slices.Equal(l.LowSampleTargets, []string{"amazon_pdp"}) || l.TargetsCounted != 8 || !l.CostPer1k.OK() {
		t.Errorf("low %+v", l)
	}
	g := rows["gap"]
	if g.CostPer1k.OK() || !vaNear(g.PartialCostPer1k, 0.5) || g.TargetsCounted != 7 || !slices.Equal(g.MissingTargets, []string{"google_search"}) ||
		g.ReasonOf("costPer1k") != NAIncompleteTargets || g.ReasonOf("byTarget.google_search.costPer1k") != NANoSuccesses {
		t.Errorf("gap %+v", g)
	}
	n := rows["nopr"]
	if n.CostPer1k.OK() || n.PartialCostPer1k.OK() || n.TargetsCounted != 0 || len(n.MissingTargets) != 0 ||
		n.ReasonOf("costPer1k") != NACatalog || n.ReasonOf("byTarget.ai_answers.costPer1k") != NACatalog || n.ByTarget["ai_answers"].Tries != 100 {
		t.Errorf("nopr %+v", n)
	}
	if v.Best == nil || v.Best.Slug != "low" || v.Worst == nil || v.Worst.Slug != "dear" || !vaNear(v.SpreadX, v.Worst.CostPer1k/v.Best.CostPer1k) ||
		v.Incomplete != 1 || v.Unpriced != 1 {
		t.Errorf("summary %+v %+v %+v", v.Best, v.Worst, v)
	}
}

// homeInsights fills valueAll from the measured residential rows only.
func TestHomeInsightsValueAll(t *testing.T) {
	t.Parallel()
	c := vaContext(vaEntry("a", 1, 1, nil), vaEntry("dc", 1, 1, nil), vaEntry("cat", 1, 1, nil))
	rows := vaRows("a", "dc", "cat")
	rows[1].Kind, rows[2].Source = "datacenter", "catalog"
	if v := homeInsights(c, rows, "30d").ValueAll; len(v.Rows) != 1 || v.Rows[0].Slug != "a" || v.Best == nil {
		t.Errorf("valueAll %+v", v)
	}
}

// Ties keep the rows' order; incomplete rows sort by targets counted, then
// partial bill; best, worst and spreadX are null without a complete priced row.
func TestValueAllSummaries(t *testing.T) {
	t.Parallel()
	tie := homeValueAll(vaContext(vaEntry("x", 1, 2, nil), vaEntry("y", 2, 2, nil)), vaRows("y", "x"), "30d")
	if tie.Best.Slug != "y" || tie.Worst.Slug != "x" || tie.SpreadX != 1 || tie.Incomplete != 0 || tie.Unpriced != 0 {
		t.Errorf("tie %+v %+v %+v", tie.Best, tie.Worst, tie)
	}
	none := homeValueAll(vaContext(vaEntry("x", nan.F(), 1, nil), vaEntry("y", nan.F(), 1, nil)), vaRows("x", "y"), "30d")
	if none.Best != nil || none.Worst != nil || none.SpreadX.OK() || none.Unpriced != 2 || none.naCode() != NACatalog {
		t.Errorf("none priced %+v", none)
	}
	one := map[string]snap.Metrics{"ai_answers": vaMetrics(100, 0, 1e6)}
	two := map[string]snap.Metrics{"ai_answers": vaMetrics(0, 0, 0), "amazon_pdp": vaMetrics(100, 0, 1e6)}
	all := homeValueAll(vaContext(vaEntry("two", 1, 1, two), vaEntry("dear", 1, 3, one), vaEntry("cheap", 1, 2, one)),
		vaRows("two", "dear", "cheap"), "30d")
	if got := slugsOf(all.Rows, func(r ValueAllRow) string { return r.Slug }); !sameSlugs(got, "cheap", "dear", "two") ||
		all.Best != nil || all.SpreadX.OK() || all.Incomplete != 3 || all.naCode() != NAIncompleteTargets {
		t.Errorf("all incomplete %v %+v", got, all)
	}
	if two := vaBySlug(all)["two"]; two.ReasonOf("byTarget.ai_answers.mbPerTry") != NANoRun ||
		two.ReasonOf("byTarget.amazon_pdp.costPer1k") != NANoSuccesses {
		t.Errorf("two %+v", two.NAList)
	}
	if empty := homeValueAll(vaContext(), nil, "30d"); empty.Rows == nil || empty.naCode() != NANoRun {
		t.Errorf("no row %+v", empty)
	}
}

// The home's na lists valueAll's null summary fields with a reason and
// leaves the rows, which carry their own, out.
func TestHomeNAOfValueAll(t *testing.T) {
	t.Parallel()
	one := map[string]snap.Metrics{"ai_answers": vaMetrics(100, 0, 1e6)}
	a := &HomeAnswer{Networks: []HomeNetwork{}}
	a.Insights = homeInsights(vaContext(vaEntry("x", 1, 1, one)), vaRows("x"), "30d")
	if err := a.listNA(nil); err != nil {
		t.Fatal(err)
	}
	if a.ReasonOf("insights.valueAll.best") != NAIncompleteTargets || a.ReasonOf("insights.valueAll.spreadX") != NAIncompleteTargets {
		t.Errorf("na %+v", a.NAList)
	}
	for _, p := range a.NA {
		if strings.HasPrefix(p, "insights.valueAll.rows") {
			t.Errorf("row path %s in the home's na", p)
		}
	}
}

// Bills that differ only in the 5th decimal serialize differently, and a
// complete row serializes an empty na.
func TestValueAllPrecision(t *testing.T) {
	t.Parallel()
	v := homeValueAll(vaContext(vaEntry("a", 1, 3.59821, nil), vaEntry("b", 1, 3.60154, nil)), vaRows("a", "b"), "30d")
	best, _ := json.Marshal(v.Best)
	worst, _ := json.Marshal(v.Worst)
	row, err := json.Marshal(v.Rows[0])
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(best), `"costPer1k":3.59821`) || !strings.Contains(string(worst), `"costPer1k":3.60154`) ||
		!strings.Contains(string(row), `"na":[]`) || strings.Contains(string(row), "naReason") {
		t.Errorf("best %s, worst %s, row %s", best, worst, row)
	}
}
