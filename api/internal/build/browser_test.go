package build

import (
	"encoding/json"
	"reflect"
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
