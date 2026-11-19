package build

import (
	"encoding/json"
	"errors"
	"math"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"

	"github.com/shifter-io/atlas/api/internal/model"
	"github.com/shifter-io/atlas/api/internal/snap"
)

// scores is a TARGET-SCORES-1 scores object with one status for every metric.
func scores(status string, rankedOf string) string {
	m := `{"rate": 0.5, "score": null, "rank": null, "ranked_of": ` + rankedOf + `, "status": "` + status + `"}`
	return `{"success": ` + m + `, "challenge": ` + m + `, "block": ` + m + `}`
}

// Network A: Camoufox is its main engine on amazon_pdp (all_similar), with
// Chrome tries too; 24h has no wire bytes. Network B: Chrome is its main
// engine (ranked of 3), too few Camoufox tries; no 24h window.
var targetsNetA = `{"id": "1:a", "kind": "residential", "windows": {
 "30d": {"scores": {"balanced": {"score": 70, "rank": 2}},
  "browser_overall": {"camoufox": {"success": {"score": 6, "rank": 1, "ranked_of": 2, "status": "ranked"}, "targets": ["amazon_pdp"], "weights": {"amazon_pdp": 2}}},
  "targets": {
   "generic_http": {"metrics": {"completed": 1000, "success_rate": 0.9, "request_success_rate": 0.8, "challenge_rate": 0.01, "block_rate": 0.02,
    "bytes_up": 0, "bytes_down": 1073741824, "latency_ms": {"p50": 300, "p90": 600}}},
   "amazon_pdp": {"metrics": {"completed": 200, "success_rate": 0.6}, "browser": {"engine": "camoufox", "outcome_rates": {"ok": 0.6},
    "page_load_ms": {"p50": 2000, "p90": 4000}, "scores": ` + scores("all_similar", "null") + `, "engines": {
     "camoufox": {"completed": 200, "successes": 120, "outcome_rates": {"ok": 0.6}, "page_load_ms": {"p50": 2000}, "scores": ` + scores("all_similar", "null") + `},
     "chrome": {"completed": 100, "successes": 90, "outcome_rates": {"ok": 0.9}, "page_load_ms": {"p50": 1500}, "scores": ` + scores("ranked", "3") + `}}}},
   "ai_answers": {"metrics": {"completed": 30, "success_rate": 0.5}, "browser": {"engine": "camoufox", "sources": ["ai_overview", "ai_chatgpt"],
    "engines": {"camoufox": {"completed": 30, "successes": 15}}}},
   "ai_overview": {"metrics": {"completed": 20, "success_rate": 0.4}, "browser": {"group": "ai_answers", "engine": "camoufox",
    "engines": {"camoufox": {"completed": 20, "successes": 8}}}}}},
 "24h": {"scores": {"balanced": {"score": 50, "rank": 1}},
  "targets": {"generic_http": {"metrics": {"completed": 100, "success_rate": 0.5, "bytes_up": null, "bytes_down": null}}}}}}`

var targetsNetB = `{"id": "2:b", "kind": "residential", "windows": {
 "30d": {"scores": {"balanced": {"score": 90, "rank": 1}},
  "targets": {
   "generic_http": {"metrics": {"completed": 500, "success_rate": 0.95, "request_success_rate": 0.95, "bytes_up": 536870912, "bytes_down": 0}},
   "amazon_pdp": {"metrics": {"completed": 150, "success_rate": 0.8}, "browser": {"engine": "chrome", "scores": ` + scores("ranked", "3") + `, "engines": {
     "chrome": {"completed": 150, "successes": 120, "scores": ` + scores("ranked", "3") + `},
     "camoufox": {"completed": 50, "successes": 20, "scores": ` + scores("not_enough_data", "null") + `}}}}}}}}`

var targetsNetC = `{"id": "3:c", "kind": "datacenter", "windows": {"30d": {"targets": {"generic_http": {"metrics": {"completed": 10, "success_rate": 1}}}}}}`

func targetsEntry(t *testing.T, slug, kind, product string) *entry {
	t.Helper()
	var p snap.Product
	if err := json.Unmarshal([]byte(product), &p); err != nil {
		t.Fatal(err)
	}
	pm := &model.ProductMeasurement{Kind: kind}
	// The cells' costs use the basis (4), never the display rate (9).
	pm.Pricing = model.Pricing{PricePerGb: nan, PricePerGbAtScale: 9, BasisPerGb: 4}
	measurements := model.NewMeasurementMap()
	measurements.Set(kind, pm)
	return &entry{P: &model.Provider{Slug: slug, Name: strings.ToUpper(slug), ProductMeasurements: measurements},
		Snap: map[string]*snap.Product{kind: &p}}
}

func targetsContext(t *testing.T) *Context {
	t.Helper()
	var s snap.Snapshot
	if err := json.Unmarshal([]byte(`{"methodology": {"id": "m", "settings": {
 "browser_difficulty": {"amazon_pdp": 2, "ai_overview": 4, "ai_chatgpt": 3},
 "target_groups": {"ai_answers": ["ai_overview", "ai_chatgpt"]}}}}`), &s); err != nil {
		t.Fatal(err)
	}
	browser, http, group := model.S(model.EngineBrowser), model.S(model.EngineHTTP), model.S("ai_answers")
	targets := []model.TargetMeta{
		{ID: "generic_http", Label: model.S("Open page"), Difficulty: 1, Source: model.SourceMeasured, Engine: http},
		{ID: "amazon_pdp", Label: model.S("Amazon"), Difficulty: nan, Source: model.SourceMeasured, Engine: browser},
		{ID: "ai_answers", Label: model.S("AI answers"), Difficulty: nan, Source: model.SourceMeasured, Engine: browser,
			Sources: []string{"ai_overview", "ai_chatgpt"}},
		{ID: "ai_overview", Label: model.S("AI Overview"), Difficulty: nan, Source: model.SourceMeasured, Engine: browser, Group: group},
		{ID: "ai_chatgpt", Label: model.S("ChatGPT"), Difficulty: nan, Source: model.SourceMeasured, Engine: browser, Group: group},
		{ID: "ticketing_queue", Label: model.S("Ticketing"), Difficulty: 5, Source: "catalog"},
	}
	return &Context{GeneratedAt: "2026-11-08T00:00:00.000Z", Snapshot: &s, Dataset: &model.Dataset{Targets: targets},
		Entries: []*entry{targetsEntry(t, "a", "residential", targetsNetA), targetsEntry(t, "b", "residential", targetsNetB),
			targetsEntry(t, "c", "datacenter", targetsNetC)}}
}

func targetsBodies(t *testing.T) map[string]*TargetsAnswer {
	t.Helper()
	bodies, err := buildTargets(targetsContext(t))
	if err != nil {
		t.Fatal(err)
	}
	out := map[string]*TargetsAnswer{}
	for path, b := range bodies {
		out[path] = b.(*TargetsAnswer)
	}
	if len(out) != len(model.ProxyKinds)*len(Ranges)*(1+len(TargetEngines)) {
		t.Fatalf("%d bodies", len(out))
	}
	return out
}

func targetOf(ans *TargetsAnswer, id string) TargetEntry {
	for _, te := range ans.Targets {
		if te.ID == id {
			return te
		}
	}
	return TargetEntry{}
}

func near(a Num, b float64) bool { return a.OK() && math.Abs(a.F()-b) < 1e-9 }

func TestTheTargetsAnswerListsEveryTargetsMeta(t *testing.T) {
	t.Parallel()
	ans := targetsBodies(t)["/v1/targets/residential/30d"]
	if ans.GeneratedAt != "2026-11-08T00:00:00.000Z" || ans.Range != "30d" || ans.Kind != "residential" || ans.CardCount != 3 {
		t.Fatalf("answer %+v", ans)
	}
	ids := []string{}
	for _, te := range ans.Targets {
		ids = append(ids, te.ID)
	}
	if !reflect.DeepEqual(ids, []string{"generic_http", "amazon_pdp", "ai_answers", "ai_overview", "ai_chatgpt", "ticketing_queue"}) {
		t.Errorf("order %v", ids)
	}
	// The catalog's difficulty stays; without one it is the scoring weight.
	if h := targetOf(ans, "generic_http"); h.Difficulty != 1 || h.ScoreWeight.OK() || !h.Card || model.SV(h.Engine) != "http" {
		t.Errorf("generic_http %+v", h)
	}
	if a := targetOf(ans, "amazon_pdp"); a.Difficulty != 2 || a.ScoreWeight != 2 || !a.Card {
		t.Errorf("amazon_pdp %+v", a)
	}
	// The AI group: a card with its sources, weighted as its hardest source.
	g := targetOf(ans, "ai_answers")
	if !g.Card || g.ScoreWeight != 4 || g.Difficulty != 4 || !reflect.DeepEqual(g.Sources, []string{"ai_overview", "ai_chatgpt"}) || g.Group != nil {
		t.Errorf("ai_answers %+v", g)
	}
	if s := targetOf(ans, "ai_chatgpt"); s.Card || model.SV(s.Group) != "ai_answers" || s.ScoreWeight != 3 || len(s.Sources) != 0 || s.Sources == nil {
		t.Errorf("ai_chatgpt %+v", s)
	}
	// An unmeasured (catalog-only) target: no card, no cell, no fleet.
	u := targetOf(ans, "ticketing_queue")
	if u.Card || u.Engine != nil || u.Difficulty != 5 || u.ScoreWeight.OK() || len(u.Fleet) != 0 || len(u.ScoreStatus) != 0 {
		t.Errorf("ticketing_queue %+v", u)
	}
	for _, n := range ans.Networks {
		if _, ok := n.Cells["ticketing_queue"]; ok || len(n.Cells) != 5 {
			t.Errorf("%s cells %v", n.Slug, n.Cells)
		}
	}
}

func TestTheTargetsAnswerOrdersTheNetworksOfItsKind(t *testing.T) {
	t.Parallel()
	bodies := targetsBodies(t)
	slugs := func(ans *TargetsAnswer) (out []string) {
		for _, n := range ans.Networks {
			out = append(out, n.Slug)
		}
		return out
	}
	month, day := bodies["/v1/targets/residential/30d"], bodies["/v1/targets/residential/24h"]
	if !reflect.DeepEqual(slugs(month), []string{"b", "a"}) || month.Networks[0].Position != 1 || month.Networks[0].Rank != 1 ||
		month.Networks[1].Position != 2 || month.Networks[1].Rank != 2 || month.Networks[1].Name != "A" {
		t.Errorf("30d networks %+v", month.Networks)
	}
	// 24h: only A has a score, so it leads.
	if !reflect.DeepEqual(slugs(day), []string{"a", "b"}) || day.Networks[1].Rank.OK() {
		t.Errorf("24h networks %v", slugs(day))
	}
	if dc := bodies["/v1/targets/datacenter/30d"]; !reflect.DeepEqual(slugs(dc), []string{"c"}) {
		t.Errorf("datacenter %v", slugs(dc))
	}
	if isp := bodies["/v1/targets/isp/7d"]; len(isp.Networks) != 0 || isp.Networks == nil || isp.CardCount != 3 {
		t.Errorf("isp %+v", isp)
	}
	a := month.Networks[1]
	if o := a.BrowserOverall["camoufox"].Success; o.Score != 6 || o.Rank != 1 || o.RankedOf != 2 || o.Status != "ranked" {
		t.Errorf("browserOverall %+v", a.BrowserOverall)
	}
	if month.Networks[0].BrowserOverall != nil {
		t.Errorf("b browserOverall %+v", month.Networks[0].BrowserOverall)
	}
}

// A browser target with two engines: in the default body each network's cell
// is its main engine's, naming the other; the means, statuses and ranks never
// add Chrome and Camoufox together.
func TestABrowserTargetKeepsItsEnginesApart(t *testing.T) {
	t.Parallel()
	ans := targetsBodies(t)["/v1/targets/residential/30d"]
	b, a := ans.Networks[0].Cells["amazon_pdp"], ans.Networks[1].Cells["amazon_pdp"]
	if ans.Engine != nil || model.SV(a.Engine) != "camoufox" || a.SuccessRate != 0.6 || a.Requests != 200 || a.PageLoadP50 != 2000 ||
		a.Scores.Success.Status != "all_similar" || a.Outcomes.Ok != 0.6 || !reflect.DeepEqual(a.OtherEngines, []string{"chrome"}) {
		t.Errorf("a amazon_pdp %+v", a)
	}
	if model.SV(b.Engine) != "chrome" || b.SuccessRate != 0.8 || !reflect.DeepEqual(b.OtherEngines, []string{"camoufox"}) {
		t.Errorf("b amazon_pdp %+v", b)
	}
	// Each is alone with its engine among the networks.
	for _, c := range []*TargetCell{a, b} {
		if c.SuccessRank != (model.Rank{Rank: 1, Peers: 1}) || c.CostRank.Rank.OK() || c.CostRank.Peers != 0 {
			t.Errorf("ranks %+v %+v", c.SuccessRank, c.CostRank)
		}
	}
	if s := ans.Networks[1].Cells["ai_overview"]; s.OtherEngines == nil || len(s.OtherEngines) != 0 {
		t.Errorf("one engine: otherEngines %v", s.OtherEngines)
	}
	fleet := targetOf(ans, "amazon_pdp").Fleet
	if len(fleet) != 2 || !near(fleet["camoufox"].MeanSuccess, 0.5) || fleet["camoufox"].Networks != 2 ||
		!near(fleet["chrome"].MeanSuccess, 0.85) || fleet["chrome"].Networks != 2 {
		t.Errorf("fleet %+v", fleet)
	}
	// The fleet's score is absolute: 1 + 9 × its mean, to 1 decimal.
	// Its mean is internal (SUCCESS-RATE-REMOVE-1, #900).
	if got := jsonString(t, fleet["camoufox"]); got != `{"successScore":{"score":5.5,"status":"ranked","basis":"mean_success_rate"},"networks":2}` {
		t.Errorf("camoufox fleet %s", got)
	}
	// all_similar is the status every Camoufox network with enough tries
	// shares; B's not_enough_data does not hide it.
	status := targetOf(ans, "amazon_pdp").ScoreStatus
	if status["camoufox"].Status != "all_similar" || status["camoufox"].RankedOf.OK() || status["chrome"] != (ScoreStatus{"ranked", 3}) {
		t.Errorf("scoreStatus %+v", status)
	}
	// A network target's status is http's: not_enough_data without scores.
	if s := targetOf(ans, "generic_http").ScoreStatus; len(s) != 1 || s["http"].Status != notEnoughData || s["http"].RankedOf.OK() {
		t.Errorf("http scoreStatus %+v", s)
	}
}

// An engine body: every browser cell is that engine's (nulls without its
// tries), ranked among the networks with tries of it; the meta is the same.
func TestAnEngineBodyIsThatEnginesFigures(t *testing.T) {
	t.Parallel()
	bodies := targetsBodies(t)
	month, chrome := bodies["/v1/targets/residential/30d"], bodies["/v1/targets/residential/30d/chrome"]
	if model.SV(chrome.Engine) != "chrome" || !reflect.DeepEqual(chrome.Targets, month.Targets) || chrome.CardCount != 3 {
		t.Errorf("chrome body %+v", chrome)
	}
	b, a := chrome.Networks[0].Cells["amazon_pdp"], chrome.Networks[1].Cells["amazon_pdp"]
	if model.SV(a.Engine) != "chrome" || a.SuccessRate != 0.9 || a.Requests != 100 || a.Scores.Success.Status != "ranked" ||
		a.PageLoadP50 != 1500 || !reflect.DeepEqual(a.OtherEngines, []string{"camoufox"}) || a.SuccessRank != (model.Rank{Rank: 1, Peers: 1}) {
		t.Errorf("a chrome %+v", a)
	}
	// B's Chrome tries publish no outcome rates: no rate to rank.
	if b.Requests != 150 || b.SuccessRate.OK() || b.SuccessRank.Rank.OK() || b.SuccessRank.Peers != 1 {
		t.Errorf("b chrome %+v", b)
	}
	// A has no Chrome tries on ai_overview: nulls, 0 tries, the engine named.
	if o := chrome.Networks[1].Cells["ai_overview"]; model.SV(o.Engine) != "chrome" || o.Requests != 0 || o.SuccessRate.OK() ||
		o.Outcomes.Ok.OK() || o.PageLoadP50.OK() || o.Scores != nil || o.CostPer1kSuccess.OK() || !reflect.DeepEqual(o.OtherEngines, []string{"camoufox"}) {
		t.Errorf("ai_overview chrome %+v", o)
	}
	camoufox := bodies["/v1/targets/residential/30d/camoufox"]
	b, a = camoufox.Networks[0].Cells["amazon_pdp"], camoufox.Networks[1].Cells["amazon_pdp"]
	if a.SuccessRate != 0.6 || a.Requests != 200 || b.Requests != 50 || b.Scores.Success.Status != "not_enough_data" {
		t.Errorf("camoufox a %+v, b %+v", a, b)
	}
	// Network targets are the same in every body.
	if h := camoufox.Networks[1].Cells["generic_http"]; h.LatencyP50 != 300 || !near(h.CostPer1kSuccess, 4*1.073741824/0.8) || h.SuccessRank != (model.Rank{Rank: 2, Peers: 2}) {
		t.Errorf("generic_http camoufox %+v", h)
	}
}

func TestANetworkTargetCellHasItsCostAndRanks(t *testing.T) {
	t.Parallel()
	bodies := targetsBodies(t)
	month := bodies["/v1/targets/residential/30d"]
	b, a := month.Networks[0].Cells["generic_http"], month.Networks[1].Cells["generic_http"]
	// 1 GiB (1.073741824 GB of 10^9 bytes) of wire traffic per 1,000 requests at
	// $4/GB over 0.8 and 0.95 request success, unrounded.
	if !near(a.CostPer1kSuccess, 4*1.073741824/0.8) || !near(b.CostPer1kSuccess, 4*1.073741824/0.95) {
		t.Errorf("cost a %v, b %v", a.CostPer1kSuccess, b.CostPer1kSuccess)
	}
	if a.SuccessRank != (model.Rank{Rank: 2, Peers: 2}) || b.SuccessRank != (model.Rank{Rank: 1, Peers: 2}) ||
		a.CostRank != (model.Rank{Rank: 2, Peers: 2}) || b.CostRank != (model.Rank{Rank: 1, Peers: 2}) {
		t.Errorf("ranks a %+v %+v, b %+v %+v", a.SuccessRank, a.CostRank, b.SuccessRank, b.CostRank)
	}
	if a.LatencyP50 != 300 || a.LatencyP90 != 600 || a.CaptchaRate != 0.01 || a.BlockRate != 0.02 || a.Requests != 1000 || a.BrowserCell != nil {
		t.Errorf("a generic_http %+v", a)
	}
	if f := targetOf(month, "generic_http").Fleet; len(f) != 1 || !near(f["http"].MeanSuccess, 0.925) || f["http"].Networks != 2 ||
		jsonString(t, f["http"].SuccessScore) != `{"score":9.3,"status":"ranked","basis":"mean_success_rate"}` {
		t.Errorf("http fleet %+v", f)
	}
	// 24h is its own window: other rates, no wire bytes, B unmeasured.
	day := bodies["/v1/targets/residential/24h"]
	a, b = day.Networks[0].Cells["generic_http"], day.Networks[1].Cells["generic_http"]
	if a.SuccessRate != 0.5 || a.Requests != 100 || a.CostPer1kSuccess.OK() || a.CostRank.Rank.OK() || a.SuccessRank != (model.Rank{Rank: 1, Peers: 1}) {
		t.Errorf("24h a %+v", a)
	}
	if b.SuccessRate.OK() || b.Requests != 0 || b.SuccessRank.Rank.OK() {
		t.Errorf("24h b %+v", b)
	}
	if f := targetOf(day, "generic_http").Fleet; !near(f["http"].MeanSuccess, 0.5) || f["http"].Networks != 1 {
		t.Errorf("24h fleet %+v", f)
	}
}

// The JSON writes one engine's figures, flat, on browser cells and latency on
// network cells only; a group's sources are cells of their own.
func TestTheTargetsAnswerJSON(t *testing.T) {
	t.Parallel()
	raw, err := json.Marshal(targetsBodies(t)["/v1/targets/residential/30d"])
	if err != nil {
		t.Fatal(err)
	}
	var ans struct {
		Networks []struct {
			Cells map[string]map[string]any `json:"cells"`
		} `json:"networks"`
	}
	if err := json.Unmarshal(raw, &ans); err != nil {
		t.Fatal(err)
	}
	cells := ans.Networks[1].Cells
	if _, ok := cells["generic_http"]["engine"]; ok || !near(Num(cells["generic_http"]["costPer1kSuccess"].(float64)), 4*1.073741824/0.8) || len(cells["generic_http"]) != 8 ||
		cells["generic_http"]["scores"] != nil {
		t.Errorf("generic_http %v", cells["generic_http"])
	}
	for _, id := range []string{"amazon_pdp", "ai_answers"} {
		c := cells[id]
		_, engines := c["engines"]
		_, latency := c["latencyP50"]
		if engines || latency || c["engine"] != "camoufox" || c["costPer1kSuccess"] != nil || len(c) != 11 {
			t.Errorf("%s %v", id, c)
		}
	}
	_, rate := cells["ai_overview"]["successRate"]
	if o := cells["ai_overview"]; rate || o["requests"] != 20.0 || cells["ai_chatgpt"]["requests"] != 0.0 {
		t.Errorf("sources %v %v", o, cells["ai_chatgpt"])
	}
	if !strings.Contains(string(raw), `"scoreStatus":{"camoufox":{"status":"all_similar","rankedOf":null}`) {
		t.Errorf("scoreStatus not in %s", raw)
	}
}

func TestTheTargetsKey(t *testing.T) {
	t.Parallel()
	for target, want := range map[string]string{"/v1/targets": "/v1/targets/residential/30d", "/v1/targets?range=24h": "/v1/targets/residential/24h",
		"/v1/targets?kind=isp&range=7d": "/v1/targets/isp/7d", "/v1/targets?kind=mobile": "/v1/targets/mobile/30d",
		"/v1/targets?engine=chrome": "/v1/targets/residential/30d/chrome", "/v1/targets?range=7d&engine=camoufox": "/v1/targets/residential/7d/camoufox"} {
		if got, err := targetsKey(httptest.NewRequest(http.MethodGet, target, nil)); got != want || err != nil {
			t.Errorf("%s: %s %v", target, got, err)
		}
	}
	for _, target := range []string{"/v1/targets?range=90d", "/v1/targets?kind=browser", "/v1/targets?kind=Res!", "/v1/targets?slug=a",
		"/v1/targets?engine=http", "/v1/targets?engine=Chrome"} {
		var bad APIError
		if _, err := targetsKey(httptest.NewRequest(http.MethodGet, target, nil)); !errors.As(err, &bad) {
			t.Errorf("%s: %v", target, err)
		}
	}
	if _, err := buildTargets(&Context{}); err == nil {
		t.Error("built without a dataset")
	}
	if w := scoreWeights(nil); len(w) != 0 {
		t.Errorf("no snapshot: %v", w)
	}
	// A group's own difficulty wins over its sources'.
	var s snap.Snapshot
	s.Methodology.Settings.BrowserDifficulty = map[string]float64{"ai_answers": 1, "ai_overview": 4}
	s.Methodology.Settings.TargetGroups = map[string][]string{"ai_answers": {"ai_overview"}}
	if w := scoreWeights(&s); w["ai_answers"] != 1 || w["ai_overview"] != 4 || len(w) != 2 {
		t.Errorf("own difficulty: %v", w)
	}
}
