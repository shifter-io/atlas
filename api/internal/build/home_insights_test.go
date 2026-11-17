package build

import (
	"encoding/json"
	"slices"
	"testing"

	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/snap"
)

func insightRow(slug, source, kind string, authenticity, exclusive, costPer1k float64) RankingRow {
	r := RankingRow{Slug: slug, Name: "Net " + slug, Source: source, Kind: kind, CostPer1k: Num(costPer1k)}
	r.Authenticity, r.ExclusiveShare = Num(authenticity), Num(exclusive)
	return r
}

func slugsOf[T any](items []T, slug func(T) string) []string {
	out := []string{}
	for _, it := range items {
		out = append(out, slug(it))
	}
	return out
}

func sameSlugs(got []string, want ...string) bool { return slices.Equal(got, want) }

// insightRows are ovContext's residential networks as measured ranking rows,
// with a datacenter network and an unmeasured one that ranks first on every
// figure.
func insightRows() []RankingRow {
	m, res := model.SourceMeasured, "residential"
	return []RankingRow{
		insightRow("solo", m, res, 0.99, 1, 0.004), insightRow("resold", m, res, 0.9, 0.35, 0.001),
		insightRow("big", m, res, 0.95, 0.93, nan.F()), insightRow("mixed", m, res, nan.F(), 0.5, 0.002),
		insightRow("unknown", m, res, 0.8, nan.F(), nan.F()), insightRow("dc", m, "datacenter", 1, 0.7, 0.0001),
		insightRow("catalog-only", "catalog", res, 1, 1, 0.00001),
	}
}

// Authenticity is the top five measured non-datacenter rows, a missing value
// last and null; exclusivity the top five measured rows by exclusiveScore, each with its
// likely upstream in that range: resold sits inside big in 30d, not in 24h.
func TestHomeAuthenticityAndExclusivity(t *testing.T) {
	t.Parallel()
	c := insightContext()
	ins := homeInsights(c, insightRows(), "30d")
	auth := ins.Authenticity
	if !sameSlugs(slugsOf(auth, func(p HomePick) string { return p.Slug }), "solo", "big", "resold", "unknown", "mixed") ||
		auth[0].Value != 0.99 || auth[4].Value.OK() {
		t.Errorf("authenticity %+v", auth)
	}
	ex := ins.Exclusivity
	if !sameSlugs(slugsOf(ex, func(r HomeExclusiveRow) string { return r.Slug }), "big", "solo", "mixed", "resold", "dc") {
		t.Fatalf("exclusivity %+v", ex)
	}
	if up := ex[3].Upstream; up == nil || up.Slug != "big" || up.Name != "Net big" || ex[0].Upstream != nil || ex[4].Upstream != nil {
		t.Errorf("30d upstreams %+v", ex)
	}
	if ex24 := homeInsights(c, insightRows(), "24h").Exclusivity; ex24[3].Slug != "resold" || ex24[3].Upstream != nil {
		t.Errorf("24h resold %+v", ex24)
	}
	empty := homeInsights(c, nil, "30d")
	body, err := json.Marshal(empty)
	if err != nil || len(empty.Authenticity) != 0 || len(empty.Exclusivity) != 0 {
		t.Errorf("no rows: %s (%v)", body, err)
	}
}

// insightContext is ovContext with a dataset of targets: a catalog-only one
// with the highest difficulty, an http one of difficulty 1 and a browser one
// whose difficulty is its scoring weight, 4. resold, big and mixed measure the
// browser target in 30d; solo measures only the http one.
func insightContext() *Context {
	c := ovContext()
	c.Dataset = &model.Dataset{Targets: []model.TargetMeta{
		{ID: "queue", Source: "catalog", Difficulty: 5},
		{ID: "plain", Label: model.S("Plain"), Source: model.SourceMeasured, Difficulty: 1, Engine: model.S(model.EngineHTTP)},
		{ID: "shop", Label: model.S("Shop"), PassCondition: model.S("The page renders."), Source: model.SourceMeasured,
			Difficulty: nan, Engine: model.S(model.EngineBrowser)},
	}}
	c.Snapshot.Methodology.Settings.BrowserDifficulty = map[string]float64{"shop": 4}
	browser := func(rate float64, engine string, score *snap.TargetScores) snap.TargetResult {
		return snap.TargetResult{Metrics: snap.Metrics{SuccessRate: Num(rate), Completed: 10},
			Browser: &snap.BrowserResult{Engine: engine, Scores: score}}
	}
	scored := &snap.TargetScores{Success: snap.MetricScore{Score: 9, Rank: 1, RankedOf: 2, Status: "ranked"}}
	results := map[string]map[string]snap.TargetResult{
		"resold": {"shop": browser(0.6, "chromium", scored)}, "big": {"shop": browser(0.8, "firefox", nil)},
		"mixed": {"shop": browser(nan.F(), "chromium", nil)}, "solo": {"plain": {Metrics: snap.Metrics{SuccessRate: 1}}},
	}
	for _, e := range c.Entries {
		e.P.PrimaryKind = "residential"
		if p := e.Snap["residential"]; p != nil {
			w := p.Windows["30d"]
			w.Targets = results[e.P.Slug]
			p.Windows["30d"] = w
		}
	}
	return c
}

// The hardest target is the measured one with the highest difficulty (the
// scoring weight standing in for a missing catalog one), never the catalog
// one; its rates are every network's that measures it in the range, highest
// first, with its engine and success score. Its words are the client's, by
// id (TARGET-TEXT-REMOVE-1): no label or pass condition is sent.
func TestHomeHardest(t *testing.T) {
	t.Parallel()
	c := insightContext()
	ins := homeInsights(c, insightRows(), "30d")
	h := ins.Hardest
	if h == nil || h.ID != "shop" || h.Difficulty != 4 || model.SV(h.Engine) != model.EngineBrowser {
		t.Fatalf("hardest %+v", h)
	}
	if b, _ := json.Marshal(h); string(b) != `{"id":"shop","difficulty":4,"engine":"browser"}` {
		t.Errorf("hardest %s", b)
	}
	rates := ins.HardestRates
	if !sameSlugs(slugsOf(rates, func(r HomeTargetRate) string { return r.Slug }), "big", "resold") {
		t.Fatalf("rates %+v", rates)
	}
	if rates[0].SuccessRate != 0.8 || model.SV(rates[0].Engine) != "firefox" || rates[0].Scores != nil ||
		rates[1].Scores == nil || rates[1].Scores.Success.Score != 9 || rates[1].Scores.Success.Status != "ranked" {
		t.Errorf("rates %+v", rates)
	}
	if r24 := homeInsights(c, insightRows(), "24h"); r24.Hardest == nil || len(r24.HardestRates) != 0 {
		t.Errorf("24h measures no target: %+v", r24.HardestRates)
	}
}

// Without a measured target with a difficulty there is no hardest target and
// no rates; a network target's rate has the http engine and no scores.
func TestHomeHardestEmptyAndNetworkTarget(t *testing.T) {
	t.Parallel()
	c := insightContext()
	c.Snapshot.Methodology.Settings.BrowserDifficulty = nil
	c.Dataset.Targets[1].Difficulty = nan
	if ins := homeInsights(c, insightRows(), "30d"); ins.Hardest != nil || ins.HardestRates == nil || len(ins.HardestRates) != 0 {
		t.Errorf("no difficulty: %+v, %+v", ins.Hardest, ins.HardestRates)
	}
	c.Dataset.Targets[1].Difficulty = 1
	ins := homeInsights(c, insightRows(), "30d")
	if ins.Hardest == nil || ins.Hardest.ID != "plain" || len(ins.HardestRates) != 1 || ins.HardestRates[0].Slug != "solo" ||
		model.SV(ins.HardestRates[0].Engine) != model.EngineHTTP || ins.HardestRates[0].Scores != nil {
		t.Errorf("network target: %+v, %+v", ins.Hardest, ins.HardestRates)
	}
	body, err := json.Marshal(homeInsights(&Context{Dataset: &model.Dataset{}}, nil, "7d"))
	if err != nil || string(body) != `{"authenticity":[],`+
		`"hardest":null,"hardestRates":[],"exclusivity":[],"valueAll":{"unit":"usd_per_1k_successes","gbBytes":1000000000,`+
		`"weighting":"equal","targets":[],"best":null,"worst":null,"spreadX":null,"incomplete":0,"unpriced":0,"rows":[]}}` {
		t.Errorf("empty insights %s (%v)", body, err)
	}
}
