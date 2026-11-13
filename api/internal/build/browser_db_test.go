package build_test

import (
	"encoding/json"
	"os"
	"reflect"
	"slices"
	"strings"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/build"
	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/Zeus-Development/proxy-index/api/internal/testdb"
	"github.com/Zeus-Development/proxy-index/api/internal/testscale"
)

// browserBuild publishes the every-request fixture, with the nine browser
// targets' K-BP6 entries when withBrowser, to a database whose framework
// targets are Generic HTTP/HTTPS plus K-BP1's browser targets, and builds it.
func browserBuild(t *testing.T, withBrowser bool) *build.Result {
	t.Helper()
	snapshot, err := os.ReadFile(repoFile(t, "fixtures", "measured", "snapshot-every-request.json"))
	if err != nil {
		t.Fatal(err)
	}
	if withBrowser {
		if snapshot, err = testscale.WithBrowser(snapshot); err != nil {
			t.Fatal(err)
		}
	}
	db := testdb.New(t)
	db.Exec(t, testscale.BrowserTargetsSQL())
	db.PublishV4(t, snapshot)
	return buildFrom(t, db)
}

func jsonOf(t *testing.T, v any) string {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

// The dataset lists every target by sort_order with its engine, the sources
// of "AI answers" carry their group, so the cards (group null) are the two
// Generic targets and six browser cards, and no "Ticketing queue".
func TestBrowserTargetsAreListedInOrderWithEngineAndGroup(t *testing.T) {
	t.Parallel()
	res := browserBuild(t, true)
	want := []string{"generic_http", "generic_https"}
	for _, b := range testscale.BrowserTargets {
		want = append(want, b.ID)
	}
	got, cards := []string{}, []string{}
	for _, x := range res.Dataset.Targets {
		got = append(got, x.ID)
		if x.Group == nil {
			cards = append(cards, x.ID)
		}
		engine := model.EngineHTTP
		if !strings.HasPrefix(x.ID, "generic_") {
			engine = model.EngineBrowser
		}
		if model.SV(x.Engine) != engine || x.Source != model.SourceMeasured {
			t.Errorf("%s: engine %v source %s", x.ID, model.SV(x.Engine), x.Source)
		}
		for _, na := range x.NA {
			if na == "group" || na == "engine" {
				t.Errorf("%s lists %s as N/A", x.ID, na)
			}
		}
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("targets %v, want %v", got, want)
	}
	if strings.Join(cards, ",") != "generic_http,generic_https,amazon_pdp,google_search,linkedin_public,instagram_web,cloudflare_challenge,ai_answers" {
		t.Errorf("cards %v", cards)
	}
	for _, x := range res.Dataset.Targets {
		if strings.HasPrefix(x.ID, "ai_") && x.ID != testscale.GroupTarget && model.SV(x.Group) != testscale.GroupTarget {
			t.Errorf("%s group %v", x.ID, model.SV(x.Group))
		}
	}
	if strings.Contains(jsonOf(t, res.Pages), "ticketing_queue") || strings.Contains(jsonOf(t, res.Dataset), "ticketing_queue") {
		t.Error("ticketing_queue appears in measured data")
	}
	// Search lists the card targets (not the AI sources), coverage every target.
	for _, id := range want {
		found := res.Search.Search(strings.ReplaceAll(id, "_", " ")).Items
		listed := slices.ContainsFunc(found, func(it build.SearchItem) bool { return it.Type == build.SearchTarget && it.ID == id })
		if listed != slices.Contains(cards, id) {
			t.Errorf("search lists target %s: %v, a card: %v", id, listed, slices.Contains(cards, id))
		}
	}
	if len(res.Coverage.Targets) != len(want) {
		t.Errorf("coverage lists %d targets, want %d", len(res.Coverage.Targets), len(want))
	}
}

// A network answer gives each browser target its outcome shares, page loads
// and (AI answers) its three sources, while the Generic targets' entries keep
// exactly their fields and values, and their (null, unscored) success scores.
func TestANetworkAnswerCarriesTheBrowserCardFields(t *testing.T) {
	t.Parallel()
	res := browserBuild(t, true)
	plain := browserBuild(t, false)
	targets := res.Pages.Network["shifter/residential"].Profile.Targets
	before := plain.Pages.Network["shifter/residential"].Profile.Targets
	if len(targets) != 11 || len(before) != 11 {
		t.Fatalf("%d targets with browser entries, %d without, want 11", len(targets), len(before))
	}
	for i, b := range targets {
		if !strings.HasPrefix(b.Target, "generic_") {
			checkBrowserBenchmark(t, b.TargetBenchmark)
			continue
		}
		if got, was := jsonOf(t, b), jsonOf(t, before[i]); got != was {
			t.Errorf("%s changed: %s, was %s", b.Target, got, was)
		}
		var keys map[string]any
		if err := json.Unmarshal([]byte(jsonOf(t, b)), &keys); err != nil || len(keys) != 10 || keys["scores"] != nil {
			t.Errorf("%s has %d fields: %s", b.Target, len(keys), jsonOf(t, b))
		}
	}
	// The network's overall browser score, only when the snapshot has one:
	// every network's is 8.2, so the best of the group reads 10 (SUCCESS-SHIFT-1).
	overall := res.Pages.Network["shifter/residential"].Profile.BrowserOverall["camoufox"].Success
	if overall.Score != 10 || overall.Rank != 2.5 || overall.RankedOf != 18 || len(overall.Targets) != 3 || overall.Weights[testscale.GroupTarget] != 4 {
		t.Errorf("overall %s", jsonOf(t, overall))
	}
	if o := plain.Pages.Network["shifter/residential"].Profile.BrowserOverall; o != nil {
		t.Errorf("overall without browser entries %s", jsonOf(t, o))
	}
	if !targets[0].LatencyP50.OK() || jsonOf(t, targets[0]) != jsonOf(t, before[0]) {
		t.Errorf("generic_http latency changed: %s", jsonOf(t, targets[0]))
	}
	// Measured browser targets are not N/A; unmeasured ones are, with a reason.
	// A source's unranked scores (#898) are null with their status, as a
	// target's scores are.
	for _, na := range res.Details["shifter"].Provider.NA {
		source := strings.Contains(na, "sources") && !strings.Contains(na, ".scores.")
		if strings.Contains(na, "outcomes") || strings.Contains(na, "pageLoad") || source {
			t.Errorf("measured browser field listed as N/A: %s", na)
		}
	}
	found := false
	for _, p := range plain.Coverage.Providers {
		for _, f := range p.Sections["targets"] {
			if p.ID == "shifter" && f.Path == "targets[amazon_pdp].outcomes.loginWall" {
				found = f.Reason == build.NANotMeasured && f.Source != nil && *f.Source == "snapshot"
			}
		}
	}
	if !found {
		t.Error("an unmeasured browser target's outcomes have no N/A reason")
	}
}

func checkBrowserBenchmark(t *testing.T, b model.TargetBenchmark) {
	t.Helper()
	if b.BrowserBenchmark == nil {
		t.Fatalf("%s has no browser fields", b.Target)
	}
	counts := testscale.BrowserCounts(b.Target)
	total := 0.0
	for _, n := range counts {
		total += n
	}
	o := b.Outcomes
	got := []model.Num{o.Ok, o.Challenge, o.Block, o.LoginWall, o.Timeout, o.ProxyError, o.NoContent, o.NoAnswer, o.OtherError}
	for i, n := range counts {
		if got[i] != model.Num(n/total) {
			t.Errorf("%s outcome %s = %v, want %v", b.Target, testscale.Outcomes[i], got[i], n/total)
		}
	}
	p50, p90 := testscale.PageLoad(b.Target)
	if b.PageLoadP50 != model.Num(p50) || b.PageLoadP90 != model.Num(p90) || b.LatencyP50.OK() || b.LatencyP90.OK() {
		t.Errorf("%s page load %v/%v latency %v/%v", b.Target, b.PageLoadP50, b.PageLoadP90, b.LatencyP50, b.LatencyP90)
	}
	if b.SuccessRate != model.Num(counts[0]/total) || b.CaptchaRate != model.Num(counts[1]/total) || b.BlockRate != model.Num(counts[2]/total) || b.Requests != model.Num(total) {
		t.Errorf("%s rates %+v", b.Target, b)
	}
	s := b.Scores
	if s == nil || s.Success.Status != "ranked" || s.Success.RankedOf != 18 || !s.Success.Score.OK() || s.Challenge.Status != "ranked" ||
		s.Block.Status != "not_enough_data" || s.Block.Score.OK() || jsonOf(t, b.Engines["camoufox"].Scores) != jsonOf(t, s) {
		t.Errorf("%s scores %s", b.Target, jsonOf(t, b.BrowserBenchmark))
	}
	if b.Target != testscale.GroupTarget {
		if b.Sources != nil {
			t.Errorf("%s is not a group but has sources", b.Target)
		}
		return
	}
	sources := testscale.GroupSources()
	if len(b.Sources) != len(sources) {
		t.Fatalf("ai_answers has %d sources, want %d", len(b.Sources), len(sources))
	}
	for i, s := range b.Sources {
		c := testscale.BrowserCounts(sources[i])
		if s.Target != sources[i] || s.Requests != 100 || s.SuccessRate != model.Num(c[0]/100) || s.Outcomes.NoAnswer != model.Num(c[7]/100) ||
			strings.Contains(jsonOf(t, s), `"label"`) {
			t.Errorf("source %d: %+v", i, s)
		}
	}
}

// withoutBrowserFacts is rankings without what browser targets add to them:
// the target counts and each row's overall browser score (and its N/A).
func withoutBrowserFacts(rankings map[string]*build.RankingsAnswer) map[string]build.RankingsAnswer {
	out := map[string]build.RankingsAnswer{}
	for key, ans := range rankings {
		a := *ans
		a.Targets, a.Networks = build.TargetCounts{}, append([]build.RankingRow{}, ans.Networks...)
		for i := range a.Networks {
			a.Networks[i].BrowserOverall, a.Networks[i].NAList = nil, withoutBrowserNA(a.Networks[i].NAList)
		}
		out[key] = a
	}
	return out
}

// withoutBrowserNA is l without the overall browser score's paths, every
// other path with its reason spelled out (the most common one may change).
func withoutBrowserNA(l build.NAList) build.NAList {
	out := build.NAList{NA: []string{}, Reasons: map[string]string{}}
	for _, p := range l.NA {
		if !strings.HasPrefix(p, "browserOverall") {
			out.NA, out.Reasons[p] = append(out.NA, p), l.ReasonOf(p)
		}
	}
	return out
}

// Browser targets are per target only: rankings (but for the target counts
// and overall browser scores), fleet figures, the network list and every
// network-level figure are the same with and without them, but for the
// 8-target bill, which is over them by definition (withoutAllTargetBills).
func TestBrowserTargetsChangeNoNetworkLevelFigure(t *testing.T) {
	t.Parallel()
	with, without := browserBuild(t, true), browserBuild(t, false)
	if row := with.Pages.Rankings["30d"].Networks[0]; row.BrowserOverall == nil || without.Pages.Rankings["30d"].Networks[0].BrowserOverall != nil {
		t.Errorf("%s: overall browser score %v with browser targets", row.Slug, row.BrowserOverall)
	}
	for name, pick := range map[string]func(*build.Result) any{
		"rankings":       func(r *build.Result) any { return withoutBrowserFacts(r.Pages.Rankings) },
		"kind rankings":  func(r *build.Result) any { return withoutBrowserFacts(r.Pages.RankingsByKind) },
		"networks":       func(r *build.Result) any { return r.Pages.Networks },
		"countries":      func(r *build.Result) any { return r.Pages.Countries },
		"totals":         func(r *build.Result) any { return r.Dataset.Totals },
		"measuredTotals": func(r *build.Result) any { return r.Dataset.MeasuredTotals },
		"measuredMarket": func(r *build.Result) any { return r.Dataset.MeasuredMarket },
		"windows":        func(r *build.Result) any { return r.Details["shifter"].Windows },
		"performance":    func(r *build.Result) any { return r.Details["shifter"].Provider.Performance },
		"score":          func(r *build.Result) any { return r.Details["shifter"].Provider.Score },
		"pool":           func(r *build.Result) any { return r.Details["shifter"].Provider.Pool },
		"series":         func(r *build.Result) any { return r.Details["shifter"].Provider.Series },
	} {
		if a, b := withoutAllTargetBills(t, pick(with)), withoutAllTargetBills(t, pick(without)); a != b {
			t.Errorf("%s differ with browser targets", name)
		}
	}
}

// The snapshot methodology's target_groups marks the sources even before a
// browser run is measured: no browser entry, yet the cards stay eight.
// withTargetGroups is the every-request fixture whose methodology groups
// the browser sources under their target, without browser entries.
func withTargetGroups(t *testing.T) []byte {
	t.Helper()
	snapshot, err := os.ReadFile(repoFile(t, "fixtures", "measured", "snapshot-every-request.json"))
	if err != nil {
		t.Fatal(err)
	}
	var doc map[string]any
	if err := json.Unmarshal(snapshot, &doc); err != nil {
		t.Fatal(err)
	}
	doc["methodology"].(map[string]any)["settings"].(map[string]any)["target_groups"] = map[string]any{testscale.GroupTarget: testscale.GroupSources()}
	return []byte(jsonOf(t, doc))
}

func TestTheMethodologyMarksTheSourcesBeforeAnyBrowserRun(t *testing.T) {
	t.Parallel()
	db := testdb.New(t)
	db.Exec(t, testscale.BrowserTargetsSQL())
	db.PublishV4(t, withTargetGroups(t))
	res := buildFrom(t, db)
	cards := 0
	for _, x := range res.Dataset.Targets {
		if x.Group == nil {
			cards++
		}
	}
	group := res.Pages.Network["shifter/residential"].Profile.Targets[7]
	if cards != 8 || group.Target != testscale.GroupTarget || group.BrowserBenchmark == nil || len(group.Sources) != 3 || group.Sources[0].SuccessRate.OK() || group.Outcomes.Ok.OK() {
		t.Errorf("cards %d, group entry %s", cards, jsonOf(t, group))
	}
}

// allTargetKeys are the 8-target bill's fields (VALUE-ALL-2, VALUE-ALL-PARTIAL-1).
var allTargetKeys = map[string]bool{"costPer1kAllTargets": true, "valueRankAllTargets": true, "allTargets": true,
	"partialCostPer1kAllTargets": true, "targetsCountedAllTargets": true,
	"missingTargetsAllTargets": true}

// withoutAllTargetBills is v's JSON without allTargetKeys and their N/A
// paths, at every depth.
func withoutAllTargetBills(t *testing.T, v any) string {
	t.Helper()
	var tree any
	if err := json.Unmarshal([]byte(jsonOf(t, v)), &tree); err != nil {
		t.Fatal(err)
	}
	return jsonOf(t, stripAllTargets(tree))
}

func stripAllTargets(v any) any {
	switch x := v.(type) {
	case map[string]any:
		if na, ok := x["na"].([]any); ok {
			spellOutReasons(x, na)
		}
		for k, c := range x {
			switch {
			case allTargetKeys[k]:
				delete(x, k)
			case k == "na" || k == "naReasons":
				x[k] = withoutAllTargetPaths(c)
			default:
				x[k] = stripAllTargets(c)
			}
		}
	case []any:
		for i, c := range x {
			x[i] = stripAllTargets(c)
		}
	}
	return v
}

// withoutAllTargetPaths is an na list or naReasons map without the 8-target
// bill's paths.
func withoutAllTargetPaths(v any) any {
	bill := func(p string) bool { return strings.Contains(p, "AllTargets") || strings.Contains(p, "allTargets") }
	switch x := v.(type) {
	case []any:
		out := []any{}
		for _, p := range x {
			if s, _ := p.(string); !bill(s) {
				out = append(out, p)
			}
		}
		return out
	case map[string]any:
		for p := range x {
			if bill(p) {
				delete(x, p)
			}
		}
	}
	return v
}

// spellOutReasons gives each of an entity's na paths its reason in naReasons
// and drops naReason: the most common reason may change without the bill's paths.
func spellOutReasons(x map[string]any, na []any) {
	reasons, _ := x["naReasons"].(map[string]any)
	out := map[string]any{}
	for _, p := range na {
		s, _ := p.(string)
		if r, ok := reasons[s]; ok {
			out[s] = r
		} else {
			out[s] = x["naReason"]
		}
	}
	delete(x, "naReason")
	x["naReasons"] = out
}
