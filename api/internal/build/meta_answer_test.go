package build_test

import (
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"slices"
	"testing"

	"github.com/Zeus-Development/atlas/api/internal/build"
	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/snap"
)

// metaAnswer is the registered /v1/meta answer.
func metaAnswer(t *testing.T) build.PageAnswer {
	t.Helper()
	for _, a := range build.Answers() {
		if a.Name == "meta" {
			return a
		}
	}
	t.Fatal("/v1/meta is not registered")
	return build.PageAnswer{}
}

// metaOf is the /v1/meta body of a build.
func metaOf(t *testing.T, res *build.Result) build.MetaAnswer {
	t.Helper()
	m, ok := res.Pages.Registered["/v1/meta"].(build.MetaAnswer)
	if !ok {
		t.Fatalf("no /v1/meta body (unbuilt: %v)", res.Pages.Unbuilt)
	}
	return m
}

// fixtureSnapshot is a committed snapshot fixture, edited by edit when set.
func fixtureSnapshot(t *testing.T, name string, edit func(settings map[string]any)) []byte {
	t.Helper()
	body, err := os.ReadFile(repoFile(t, "fixtures", "measured", name))
	if err != nil {
		t.Fatal(err)
	}
	if edit == nil {
		return body
	}
	var s map[string]any
	if err := json.Unmarshal(body, &s); err != nil {
		t.Fatal(err)
	}
	edit(s["methodology"].(map[string]any)["settings"].(map[string]any))
	if body, err = json.Marshal(s); err != nil {
		t.Fatal(err)
	}
	return body
}

func sameJSON(t *testing.T, a, b any) bool {
	t.Helper()
	x, err := json.Marshal(a)
	if err != nil {
		t.Fatal(err)
	}
	y, err := json.Marshal(b)
	if err != nil {
		t.Fatal(err)
	}
	return string(x) == string(y)
}

// Both fixtures publish the methodology's grades: the answer carries them
// from A+ down to D, with the dataset's weights, profiles, targets, scopes
// and the three ranges, in well under 20 KB.
func TestMetaFromBothFixtures(t *testing.T) {
	t.Parallel()
	want := []build.GradeBand{{Grade: "A+", Min: 88}, {Grade: "A", Min: 80}, {Grade: "B+", Min: 72}, {Grade: "B", Min: 64},
		{Grade: "C+", Min: 56}, {Grade: "C", Min: 48}, {Grade: "D", Min: 0}}
	for _, name := range []string{"snapshot.json", "snapshot-every-request.json"} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			res := publishedBuild(t, fixtureSnapshot(t, name, nil))
			ds, m := res.Dataset, metaOf(t, res)
			if !slices.Equal(m.Grades, want) || slices.Contains(m.NA, "grades") {
				t.Errorf("grades %v, na %v", m.Grades, m.NA)
			}
			if m.GeneratedAt != ds.GeneratedAt || m.Snapshot.ID == nil || m.Snapshot.AsOf == nil ||
				model.SV(m.Snapshot.MethodologyID) != model.SV(ds.Snapshot.MethodologyID) || !sameJSON(t, m.Weights, ds.Weights) {
				t.Errorf("head %+v %+v, dataset %+v", m.Snapshot, m.Weights, ds.Snapshot)
			}
			if len(m.Profiles) != len(ds.Profiles) || len(m.Scopes) != len(ds.Scopes) || len(m.Ranges) != 3 {
				t.Errorf("%d profiles, %d scopes, %d ranges", len(m.Profiles), len(m.Scopes), len(m.Ranges))
			}
			if len(m.Ranges) == 3 && (m.Ranges[0].Hours != 24 || m.Ranges[1].Hours != 168 || m.Ranges[2].Hours != 720 || m.WindowHours != 24) {
				t.Errorf("ranges %+v, windowHours %v", m.Ranges, m.WindowHours)
			}
			if len(m.Targets) != len(ds.Targets) || m.TargetCounts.All != len(ds.Targets) || m.TargetCounts.Measured == 0 ||
				m.TargetCounts.Cards > m.TargetCounts.Measured {
				t.Errorf("%d targets, counts %+v", len(m.Targets), m.TargetCounts)
			}
			for i, mt := range m.Targets {
				if mt.ID != ds.Targets[i].ID || mt.Card != (mt.Source == model.SourceMeasured && mt.Group == nil) {
					t.Errorf("target %d: %+v", i, mt)
				}
			}
			body, err := json.Marshal(m)
			if err != nil || len(body) >= 20<<10 {
				t.Errorf("%d bytes, %v", len(body), err)
			}
			t.Logf("%d bytes", len(body))
		})
	}
}

// /v1/meta targetCounts and /v1/rankings targets count with one rule, so one
// build gives them alike: a catalog-only target outside any group (the test
// catalog's google_search) is not a card in either.
func TestMetaAndRankingsCountTargetsAlike(t *testing.T) {
	t.Parallel()
	res := publishedBuild(t, fixtureSnapshot(t, "snapshot.json", nil))
	m, r := metaOf(t, res), res.Pages.Rankings["30d"]
	if r == nil {
		t.Fatal("no /v1/rankings?range=30d answer")
	}
	if r.Targets != m.TargetCounts {
		t.Errorf("rankings targets %+v, meta targetCounts %+v", r.Targets, m.TargetCounts)
	}
	catalogOnly := false
	for _, mt := range m.Targets {
		catalogOnly = catalogOnly || (mt.Source != model.SourceMeasured && mt.Group == nil)
	}
	if !catalogOnly {
		t.Errorf("the fixture has no catalog-only target outside a group: %+v", m.Targets)
	}
}

// A snapshot that publishes no grades gives null grades, listed in na.
func TestMetaWithoutGrades(t *testing.T) {
	t.Parallel()
	res := publishedBuild(t, fixtureSnapshot(t, "snapshot-every-request.json", func(s map[string]any) { delete(s, "grades") }))
	m := metaOf(t, res)
	body, err := json.Marshal(m)
	if err != nil {
		t.Fatal(err)
	}
	var raw map[string]json.RawMessage
	if err := json.Unmarshal(body, &raw); err != nil {
		t.Fatal(err)
	}
	if string(raw["grades"]) != "null" || !slices.Contains(m.NA, "grades") {
		t.Errorf("grades %s, na %v", raw["grades"], m.NA)
	}
}

// Without a published snapshot the head is null and in na; the grades too.
// The profiles and scopes are the dataset's.
func TestMetaWithoutASnapshot(t *testing.T) {
	t.Parallel()
	ds := &model.Dataset{Weights: model.NewBreakdown(), Profiles: []model.RankingProfile{{ID: "balanced", Weights: model.NewBreakdown()}},
		Scopes: []model.TestScope{{ID: "residential", Targets: []string{"generic_http"}}}}
	bodies, err := metaAnswer(t).Build(&build.Context{GeneratedAt: "now", Dataset: ds})
	if err != nil {
		t.Fatal(err)
	}
	m := bodies["/v1/meta"].(build.MetaAnswer)
	if m.Grades != nil || len(m.Profiles) != 1 || m.Profiles[0].ID != "balanced" || len(m.Scopes) != 1 || m.Scopes[0].ID != "residential" {
		t.Errorf("grades %v, profiles %+v, scopes %+v", m.Grades, m.Profiles, m.Scopes)
	}
	for _, p := range []string{"grades", "snapshot.id", "snapshot.asOf", "snapshot.methodologyId", "weights.value"} {
		if !slices.Contains(m.NA, p) {
			t.Errorf("na %v lacks %s", m.NA, p)
		}
	}
	if _, err := metaAnswer(t).Build(&build.Context{}); err == nil {
		t.Error("built without a dataset")
	}
}

// Each target gets its scoring weight (a group without one takes its hardest
// source's) and its card flag; only a browser target's missing weight is
// N/A, and the counts follow.
func TestMetaTargetsWeightsAndCards(t *testing.T) {
	t.Parallel()
	s := &snap.Snapshot{}
	s.Methodology.Settings.BrowserDifficulty = map[string]float64{"amazon_pdp": 2, "ai_overview": 4, "ai_gemini": 3}
	s.Methodology.Settings.TargetGroups = map[string][]string{"ai_answers": {"ai_overview", "ai_gemini"}, "empty": {"unknown"}}
	browser, plain := model.S(model.EngineBrowser), model.S(model.EngineHTTP)
	ds := &model.Dataset{Weights: model.NewBreakdown(), Targets: []model.TargetMeta{
		{ID: "generic_http", Source: model.SourceMeasured, Engine: plain},
		{ID: "amazon_pdp", Source: model.SourceMeasured, Engine: browser},
		{ID: "ai_answers", Source: model.SourceMeasured, Engine: browser},
		{ID: "ai_overview", Source: model.SourceMeasured, Engine: browser, Group: model.S("ai_answers")},
		{ID: "linkedin_public", Source: model.SourceMeasured, Engine: browser},
		{ID: "ticketing", Source: "catalog"},
	}}
	bodies, err := metaAnswer(t).Build(&build.Context{Snapshot: s, Dataset: ds})
	if err != nil {
		t.Fatal(err)
	}
	m := bodies["/v1/meta"].(build.MetaAnswer)
	want := map[string]struct {
		weight float64
		card   bool
		na     bool
	}{"generic_http": {-1, true, false}, "amazon_pdp": {2, true, false}, "ai_answers": {4, true, false},
		"ai_overview": {4, false, false}, "linkedin_public": {-1, true, true}, "ticketing": {-1, false, false}}
	for _, mt := range m.Targets {
		w := want[mt.ID]
		weight := -1.0
		if mt.ScoreWeight.OK() {
			weight = mt.ScoreWeight.F()
		}
		if weight != w.weight || mt.Card != w.card || slices.Contains(mt.NA, "scoreWeight") != w.na {
			t.Errorf("%s: weight %v card %v na %v", mt.ID, mt.ScoreWeight, mt.Card, mt.NA)
		}
	}
	if m.TargetCounts != (build.TargetCounts{All: 6, Measured: 5, Cards: 4}) {
		t.Errorf("counts %+v", m.TargetCounts)
	}
}

// Each range carries its methodology window in hours and the answer the
// shortest; a range the snapshot has no window for, or a build without a
// snapshot, gives null hours, listed in na.
func TestMetaRangeHours(t *testing.T) {
	t.Parallel()
	ds := &model.Dataset{Weights: model.NewBreakdown()}
	for _, c := range []struct {
		name    string
		windows []float64
		hours   []float64 // -1: null
		shorter float64
	}{
		{"published", []float64{24, 168, 720}, []float64{24, 168, 720}, 24},
		{"no 7-day window", []float64{720, 48}, []float64{-1, -1, 720}, 48},
		{"no snapshot", nil, []float64{-1, -1, -1}, -1},
	} {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			ctx := &build.Context{Dataset: ds}
			if c.windows != nil {
				ctx.Snapshot = &snap.Snapshot{}
				ctx.Snapshot.Methodology.Settings.Windows = c.windows
			}
			bodies, err := metaAnswer(t).Build(ctx)
			if err != nil {
				t.Fatal(err)
			}
			m := bodies["/v1/meta"].(build.MetaAnswer)
			got, null := []float64{}, false
			for _, r := range m.Ranges {
				h := -1.0
				if r.Hours.OK() {
					h = r.Hours.F()
				}
				got, null = append(got, h), null || h < 0
			}
			shortest := -1.0
			if m.WindowHours.OK() {
				shortest = m.WindowHours.F()
			}
			if !slices.Equal(got, c.hours) || shortest != c.shorter {
				t.Errorf("hours %v, windowHours %v", got, m.WindowHours)
			}
			if slices.Contains(m.NA, "ranges[].hours") != null || slices.Contains(m.NA, "windowHours") != (c.windows == nil) {
				t.Errorf("na %v", m.NA)
			}
		})
	}
}

func TestMetaKeyRefusesQueries(t *testing.T) {
	t.Parallel()
	key := metaAnswer(t).Key
	if got, err := key(httptest.NewRequest(http.MethodGet, "/v1/meta", nil)); got != "/v1/meta" || err != nil {
		t.Errorf("%q %v", got, err)
	}
	var bad build.BadRequest
	if _, err := key(httptest.NewRequest(http.MethodGet, "/v1/meta?range=7d", nil)); !errors.As(err, &bad) {
		t.Errorf("a query parameter: %v", err)
	}
}
