package build

import (
	"encoding/json"
	"reflect"
	"strings"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/Zeus-Development/proxy-index/api/internal/snap"
)

// Without a methodology target_groups, the group and sources come from the
// windows' targets.<id>.browser objects (K-BP6).
func TestTargetGroupsFromTheWindows(t *testing.T) {
	t.Parallel()
	var s snap.Snapshot
	if err := json.Unmarshal([]byte(`{"methodology": {"id": "m", "settings": {}}, "products": [{"id": "1:x", "windows": {
 "24h": {"targets": {"generic_http": {"metrics": {}},
  "ai_answers": {"metrics": {}, "browser": {"group": null, "sources": ["ai_overview", "ai_chatgpt"]}},
  "ai_overview": {"metrics": {}, "browser": {"group": "ai_answers", "sources": null}}}},
 "30d": {"targets": {"ai_chatgpt": {"metrics": {}, "browser": {"group": "ai_answers", "sources": null}}}}}}]}`), &s); err != nil {
		t.Fatal(err)
	}
	group, sources := targetGroups(&s)
	if !reflect.DeepEqual(group, map[string]string{"ai_overview": "ai_answers", "ai_chatgpt": "ai_answers"}) {
		t.Errorf("group %v", group)
	}
	if !reflect.DeepEqual(sources, map[string][]string{"ai_answers": {"ai_overview", "ai_chatgpt"}}) {
		t.Errorf("sources %v", sources)
	}
	if g, src := targetGroups(nil); len(g) != 0 || len(src) != 0 {
		t.Errorf("no snapshot: %v %v", g, src)
	}
}

// A window's browser object names its engine and keeps each engine's figures
// apart (BROWSER-ENGINE-FIGURES-1); a snapshot from before the split still
// loads, with no engine and no engines.
func TestBrowserBenchmarkEngines(t *testing.T) {
	t.Parallel()
	var w snap.Window
	if err := json.Unmarshal([]byte(`{"targets": {
 "amazon_pdp": {"metrics": {"completed": 21}, "browser": {"outcome_rates": {"ok": 0.25}, "page_load_ms": {"p50": 900, "p90": 950},
  "group": null, "sources": null, "engine": "chrome", "engines": {
   "camoufox": {"completed": 12, "successes": 9, "outcome_rates": {"ok": 0.75}, "page_load_ms": {"p50": 100, "p90": null}},
   "chrome": {"completed": 21, "successes": 6, "outcome_rates": {"ok": 0.25}, "page_load_ms": {"p50": 900, "p90": 950}}}}},
 "google_search": {"metrics": {"completed": 4}, "browser": {"outcome_rates": {"ok": 0.5}, "page_load_ms": {"p50": 300},
  "group": null, "sources": null}}}}`), &w); err != nil {
		t.Fatal(err)
	}
	b := browserBenchmark(w, model.TargetMeta{ID: "amazon_pdp"}, nil)
	if model.SV(b.Engine) != "chrome" || b.Outcomes.Ok != 0.25 || b.PageLoadP50 != 900 || len(b.Engines) != 2 {
		t.Fatalf("new payload %+v", b)
	}
	c := b.Engines["camoufox"]
	if c.Requests != 12 || c.Successes != 9 || c.Outcomes.Ok != 0.75 || c.PageLoadP50 != 100 || c.PageLoadP90.OK() || c.Outcomes.Block.OK() {
		t.Errorf("camoufox %+v", c)
	}
	if ch := b.Engines["chrome"]; ch.Requests != 21 || ch.Successes != 6 || ch.PageLoadP90 != 950 {
		t.Errorf("chrome %+v", ch)
	}
	old := browserBenchmark(w, model.TargetMeta{ID: "google_search"}, nil)
	if old.Engine != nil || old.Engines == nil || len(old.Engines) != 0 || old.Outcomes.Ok != 0.5 || old.PageLoadP50 != 300 {
		t.Errorf("old payload %+v", old)
	}
	unmeasured := browserBenchmark(w, model.TargetMeta{ID: "linkedin_public"}, nil)
	out, err := json.Marshal(unmeasured)
	if err != nil {
		t.Fatal(err)
	}
	var m map[string]any
	if err := json.Unmarshal(out, &m); err != nil || m["engine"] != nil || len(m["engines"].(map[string]any)) != 0 {
		t.Errorf("unmeasured %s", out)
	}
	if r := reasonFor("targets[amazon_pdp].engines.chrome.pageLoadP90"); r == reasonFor("targets[amazon_pdp].unknown") {
		t.Errorf("engines reason %q", r)
	}
	if r := reasonFor("targets[amazon_pdp].engine"); r == reasonFor("targets[amazon_pdp].unknown") {
		t.Errorf("engine reason %q", r)
	}
}

// scoresWindow is a 30d window with TARGET-SCORES-1's fields: amazon_pdp
// ranked (the issue's worked example: B of A-D, tied 2.5 of 4) with Chrome and
// Camoufox each scored on their own, then a not_enough_data, a
// too_few_networks and an all_similar target, and two engines' overalls.
const scoresWindow = `{"targets": {
 "amazon_pdp": {"metrics": {}, "browser": {"engine": "camoufox", "scores": {
   "success": {"rate": 0.8, "score": 5.5, "rank": 2.5, "ranked_of": 4, "status": "ranked"},
   "challenge": {"rate": 0.1, "score": 10, "rank": 1, "ranked_of": 4, "status": "ranked"},
   "block": {"rate": 0.02, "score": null, "rank": null, "ranked_of": null, "status": "all_similar"}},
  "engines": {
   "camoufox": {"completed": 500, "scores": {
    "success": {"rate": 0.8, "score": 5.5, "rank": 2.5, "ranked_of": 4, "status": "ranked"},
    "challenge": {"rate": 0.1, "score": 10, "rank": 1, "ranked_of": 4, "status": "ranked"},
    "block": {"rate": 0.02, "score": null, "rank": null, "ranked_of": null, "status": "all_similar"}}},
   "chrome": {"completed": 120, "scores": {
    "success": {"rate": 0.9, "score": 10, "rank": 1, "ranked_of": 3, "status": "ranked"},
    "challenge": {"rate": 0.05, "score": 1, "rank": 3, "ranked_of": 3, "status": "ranked"},
    "block": {"rate": 0.01, "score": 5.5, "rank": 2, "ranked_of": 3, "status": "ranked"}}}}}},
 "google_search": {"metrics": {}, "browser": {"engine": "camoufox", "scores": {
   "success": {"rate": 0.7, "score": null, "rank": null, "ranked_of": null, "status": "not_enough_data"},
   "challenge": {"rate": 0.2, "score": null, "rank": null, "ranked_of": null, "status": "not_enough_data"},
   "block": {"rate": 0.1, "score": null, "rank": null, "ranked_of": null, "status": "not_enough_data"}}}},
 "linkedin_public": {"metrics": {}, "browser": {"engine": "camoufox", "scores": {
   "success": {"rate": 0.6, "score": null, "rank": null, "ranked_of": null, "status": "too_few_networks"},
   "challenge": {"rate": 0.3, "score": null, "rank": null, "ranked_of": null, "status": "too_few_networks"},
   "block": {"rate": 0.1, "score": null, "rank": null, "ranked_of": null, "status": "too_few_networks"}}}},
 "instagram_web": {"metrics": {}, "browser": {"engine": "camoufox", "scores": {
   "success": {"rate": 0.5, "status": "all_similar"},
   "challenge": {"rate": 0.3, "status": "all_similar"},
   "block": {"rate": 0.2, "status": "all_similar"}}}}},
 "browser_overall": {
  "camoufox": {"success": {"score": 8.2, "rank": 1, "ranked_of": 4, "status": "ranked"},
   "targets": ["amazon_pdp", "google_search"], "weights": {"amazon_pdp": 2, "google_search": 3}},
  "chrome": {"success": {"score": null, "rank": null, "ranked_of": null, "status": "too_few_networks"}, "targets": [], "weights": {}}}}`

// TARGET-SCORES-API-1: each status maps to {score, rank, rankedOf, status},
// next to the raw rates, per target, per engine and per network overall.
func TestBrowserScoresMapToTheAnswer(t *testing.T) {
	t.Parallel()
	var w snap.Window
	if err := json.Unmarshal([]byte(scoresWindow), &w); err != nil {
		t.Fatal(err)
	}
	want := map[string]string{
		"amazon_pdp": `{"success":{"score":5.5,"rank":2.5,"rankedOf":4,"status":"ranked"},` +
			`"challenge":{"score":10,"rank":1,"rankedOf":4,"status":"ranked"},` +
			`"block":{"score":null,"rank":null,"rankedOf":null,"status":"all_similar"}}`,
		"google_search":   unrankedScores("not_enough_data"),
		"linkedin_public": unrankedScores("too_few_networks"),
		"instagram_web":   unrankedScores("all_similar"),
	}
	for id, s := range want {
		b := browserBenchmark(w, model.TargetMeta{ID: id}, nil)
		if got := jsonString(t, b.Scores); got != s {
			t.Errorf("%s scores %s, want %s", id, got, s)
		}
	}
	b := browserBenchmark(w, model.TargetMeta{ID: "amazon_pdp"}, nil)
	if got := jsonString(t, b.Engines["chrome"].Scores); got != `{"success":{"score":10,"rank":1,"rankedOf":3,"status":"ranked"},`+
		`"challenge":{"score":1,"rank":3,"rankedOf":3,"status":"ranked"},"block":{"score":5.5,"rank":2,"rankedOf":3,"status":"ranked"}}` {
		t.Errorf("chrome scores %s", got)
	}
	if jsonString(t, b.Engines["camoufox"].Scores) != jsonString(t, b.Scores) {
		t.Errorf("camoufox scores %s, main %s", jsonString(t, b.Engines["camoufox"].Scores), jsonString(t, b.Scores))
	}
	if got := jsonString(t, browserOverall(w)); got != `{"camoufox":{"success":{"score":8.2,"rank":1,"rankedOf":4,"status":"ranked",`+
		`"targets":["amazon_pdp","google_search"],"weights":{"amazon_pdp":2,"google_search":3}}},`+
		`"chrome":{"success":{"score":null,"rank":null,"rankedOf":null,"status":"too_few_networks","targets":[],"weights":{}}}}` {
		t.Errorf("overall %s", got)
	}
}

func unrankedScores(status string) string {
	m := `{"score":null,"rank":null,"rankedOf":null,"status":"` + status + `"}`
	return `{"success":` + m + `,"challenge":` + m + `,"block":` + m + `}`
}

func jsonString(t *testing.T, v any) string {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

// A snapshot from before target scores loads: scores null on the target and
// each engine, no overall (not written), and an overall without targets or
// weights still answers lists. Each null has its N/A reason.
func TestBrowserScoresAbsentInAnOlderSnapshot(t *testing.T) {
	t.Parallel()
	var w snap.Window
	if err := json.Unmarshal([]byte(`{"targets": {"amazon_pdp": {"metrics": {}, "browser": {"outcome_rates": {"ok": 0.5},
  "engine": "camoufox", "engines": {"camoufox": {"completed": 10}}}}}}`), &w); err != nil {
		t.Fatal(err)
	}
	b := browserBenchmark(w, model.TargetMeta{ID: "amazon_pdp"}, nil)
	if b.Scores != nil || b.Engines["camoufox"].Scores != nil || browserOverall(w) != nil {
		t.Errorf("scores %+v, engine %+v, overall %v", b.Scores, b.Engines["camoufox"].Scores, browserOverall(w))
	}
	meas := model.Measured{Targets: []model.TargetBenchmark{{Target: "amazon_pdp", BrowserBenchmark: b}}, BrowserOverall: browserOverall(w)}
	out := jsonString(t, meas)
	if strings.Contains(out, "browserOverall") || !strings.Contains(out, `"scores":null`) {
		t.Errorf("older snapshot answer %s", out)
	}
	w.BrowserOverall = map[string]snap.BrowserOverall{"chrome": {}}
	if got := jsonString(t, browserOverall(w)["chrome"].Success.Targets); got != "[]" {
		t.Errorf("overall targets %s", got)
	}
	unknown := reasonFor("targets[amazon_pdp].unknown")
	for _, path := range []string{"targets[amazon_pdp].scores", "targets[amazon_pdp].scores.success.score",
		"targets[amazon_pdp].engines.chrome.scores", "targets[amazon_pdp].engines.chrome.scores.block.rankedOf",
		"browserOverall.chrome.success.score", "productMeasurements.residential.browserOverall.chrome.success.targets"} {
		if r := reasonFor(path); r == unknown || !strings.Contains(r, "score") && !strings.Contains(r, "ranked") {
			t.Errorf("%s reason %q", path, r)
		}
	}
	if reasonFor("targets[amazon_pdp].scores") == reasonFor("targets[amazon_pdp].scores.success.rank") {
		t.Error("a missing scores object and an unranked metric share a reason")
	}
}

// A window that publishes only its engines' scores (no top-level copy, as a
// v4 network summary may: HOME-31) gives the target its main engine's.
func TestBrowserScoresFallBackToTheMainEngines(t *testing.T) {
	t.Parallel()
	var w snap.Window
	if err := json.Unmarshal([]byte(`{"targets": {"amazon_pdp": {"metrics": {}, "browser": {"outcome_rates": {"ok": 0.5},
  "engine": "camoufox", "engines": {"camoufox": {"completed": 100, "scores": {"success": {"score": 10, "rank": 1, "ranked_of": 3,
  "status": "ranked"}, "challenge": {"status": "all_similar"}, "block": {"status": "all_similar"}}},
  "chrome": {"completed": 100, "scores": {"success": {"status": "too_few_networks"}, "challenge": {"status": "too_few_networks"},
  "block": {"status": "too_few_networks"}}}}}}}}`), &w); err != nil {
		t.Fatal(err)
	}
	b := browserBenchmark(w, model.TargetMeta{ID: "amazon_pdp"}, nil)
	if b.Scores == nil || jsonString(t, b.Scores) != jsonString(t, b.Engines["camoufox"].Scores) || b.Scores.Success.Status != "ranked" {
		t.Errorf("scores %s, camoufox %s", jsonString(t, b.Scores), jsonString(t, b.Engines["camoufox"].Scores))
	}
}
