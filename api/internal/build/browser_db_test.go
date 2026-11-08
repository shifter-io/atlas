package build_test

import (
	"encoding/json"
	"os"
	"reflect"
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
	// Search and coverage list the browser targets as targets.
	search := jsonOf(t, res.Search)
	for _, id := range want {
		if !strings.Contains(search, `"target:`+id+`"`) {
			t.Errorf("search index has no target %s", id)
		}
	}
	if len(res.Coverage.Targets) != len(want) {
		t.Errorf("coverage lists %d targets, want %d", len(res.Coverage.Targets), len(want))
	}
}

// A network answer gives each browser target its outcome shares, page loads
// and (AI answers) its three sources, while the Generic targets' entries keep
// exactly their fields and values.
func TestANetworkAnswerCarriesTheBrowserCardFields(t *testing.T) {
	t.Parallel()
	res := browserBuild(t, true)
	plain := browserBuild(t, false)
	targets := res.Pages.Network["shifter/residential"].Profile.Targets
	before := plain.Pages.Network["shifter/residential"].Profile.Targets
	if len(targets) != 11 || len(before) != 11 {
		t.Fatalf("%d targets with browser entries, %d without, want 11", len(targets), len(before))
	}
	labels := map[string]string{}
	for _, b := range testscale.BrowserTargets {
		labels[b.ID] = b.Label
	}
	for i, b := range targets {
		if !strings.HasPrefix(b.Target, "generic_") {
			checkBrowserBenchmark(t, b, labels)
			continue
		}
		if got, was := jsonOf(t, b), jsonOf(t, before[i]); got != was {
			t.Errorf("%s changed: %s, was %s", b.Target, got, was)
		}
		var keys map[string]any
		if err := json.Unmarshal([]byte(jsonOf(t, b)), &keys); err != nil || len(keys) != 8 {
			t.Errorf("%s has %d fields: %s", b.Target, len(keys), jsonOf(t, b))
		}
	}
	// The network's overall browser score, only when the snapshot has one.
	overall := res.Pages.Network["shifter/residential"].Profile.BrowserOverall["camoufox"].Success
	if overall.Score != 8.2 || overall.Rank != 2.5 || overall.RankedOf != 18 || len(overall.Targets) != 3 || overall.Weights[testscale.GroupTarget] != 4 {
		t.Errorf("overall %s", jsonOf(t, overall))
	}
	if o := plain.Pages.Network["shifter/residential"].Profile.BrowserOverall; o != nil {
		t.Errorf("overall without browser entries %s", jsonOf(t, o))
	}
	if !targets[0].LatencyP50.OK() || jsonOf(t, targets[0]) != jsonOf(t, before[0]) {
		t.Errorf("generic_http latency changed: %s", jsonOf(t, targets[0]))
	}
	// Measured browser targets are not N/A; unmeasured ones are, with a reason.
	for _, na := range res.Details["shifter"].Provider.NA {
		if strings.Contains(na, "outcomes") || strings.Contains(na, "pageLoad") || strings.Contains(na, "sources") {
			t.Errorf("measured browser field listed as N/A: %s", na)
		}
	}
	found := false
	for _, p := range plain.Coverage.Providers {
		for _, f := range p.Sections["targets"] {
			if p.ID == "shifter" && f.Path == "targets[amazon_pdp].outcomes.loginWall" {
				found = strings.Contains(f.Reason, "Browser target not measured")
			}
		}
	}
	if !found {
		t.Error("an unmeasured browser target's outcomes have no N/A reason")
	}
}

func checkBrowserBenchmark(t *testing.T, b model.TargetBenchmark, labels map[string]string) {
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
	if s == nil || s.Success.Status != "ranked" || s.Success.RankedOf != 18 || !s.Success.Score.OK() || s.Challenge.Status != "all_similar" ||
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
		if s.Target != sources[i] || model.SV(s.Label) != labels[s.Target] || s.Requests != 100 || s.SuccessRate != model.Num(c[0]/100) || s.Outcomes.NoAnswer != model.Num(c[7]/100) {
			t.Errorf("source %d: %+v", i, s)
		}
	}
}

// withoutBrowserFacts is rankings without what browser targets add to them:
// the target counts and each row's overall browser score.
func withoutBrowserFacts(rankings map[string]*build.RankingsAnswer) map[string]build.RankingsAnswer {
	out := map[string]build.RankingsAnswer{}
	for key, ans := range rankings {
		a := *ans
		a.Targets, a.Networks = build.TargetCounts{}, append([]build.RankingRow{}, ans.Networks...)
		for i := range a.Networks {
			a.Networks[i].BrowserOverall = nil
		}
		out[key] = a
	}
	return out
}

// Browser targets are per target only: rankings (but for the target counts
// and overall browser scores), fleet figures, the network list and every
// network-level figure are the same with and without them.
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
		if a, b := jsonOf(t, pick(with)), jsonOf(t, pick(without)); a != b {
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
