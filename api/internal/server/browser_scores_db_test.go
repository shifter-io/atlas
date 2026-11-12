package server

import (
	"encoding/json"
	"os"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/build"
	"github.com/Zeus-Development/proxy-index/api/internal/config"
	"github.com/Zeus-Development/proxy-index/api/internal/testdb"
	"github.com/Zeus-Development/proxy-index/api/internal/testscale"
)

// browserScoresHead is the API head of a build the framework calculated
// and published (tests/test_calc_network_target_scores.py writes it): three
// residential networks, alpha, beta and gamma, with 120 Camoufox tries each
// of amazon_pdp and google_search in every window.
const browserScoresHead = "fixtures/measured/head-v4-browser-scores.json"

var (
	browserScoresTargets = []string{"amazon_pdp", "google_search"}
	browserScoresSuccess = map[string]float64{"alpha": 10, "beta": 5.5, "gamma": 1}
)

type servedScore struct {
	Score    *float64 `json:"score"`
	RankedOf *float64 `json:"rankedOf"`
	Status   string   `json:"status"`
}

type servedScores struct {
	Success servedScore `json:"success"`
}

type servedCell struct {
	Scores *servedScores `json:"scores"`
}

type servedOverall map[string]struct {
	Success servedScore `json:"success"`
}

// SCORES-V4-1: the browser target scores and browserOverall the calculator
// writes on a v4 build's network summaries are served, from the head as
// published: the targets answer's cells, in each range and for the engine,
// and the network page's profile.
func TestTheCalculatorsBrowserScoresAreServed(t *testing.T) {
	t.Parallel()
	head, err := os.ReadFile(config.FindUp(browserScoresHead))
	if err != nil {
		t.Fatal(err)
	}
	db := testdb.New(t)
	db.Exec(t, testscale.BrowserTargetsSQL())
	answers := renderedAnswers(t, db, db.Publish(t, head), nil, func(*build.Result) {})
	for _, path := range []string{"/v1/targets/residential/24h", "/v1/targets/residential/7d",
		"/v1/targets/residential/30d", "/v1/targets/residential/24h/camoufox"} {
		targetsScored(t, path, answers[path])
	}
	for slug, want := range browserScoresSuccess {
		path := "/v1/networks/" + slug + "/residential"
		if answers[path] == nil {
			t.Fatalf("no answer %s", path)
		}
		profileScored(t, slug, want, answers[path])
	}
	metaWeighted(t, head, answers["/v1/meta"])
}

// metaWeighted checks /v1/meta serves every browser target's scoreWeight as
// the head's [browser_difficulty] (the owner's fixed values,
// tests/test_target_difficulty.py), and none on an HTTP target.
func metaWeighted(t *testing.T, head, body []byte) {
	t.Helper()
	var h struct {
		Methodology struct {
			Settings struct {
				BrowserDifficulty map[string]float64 `json:"browser_difficulty"`
			} `json:"settings"`
		} `json:"methodology"`
	}
	var meta struct {
		Targets []struct {
			ID          string   `json:"id"`
			ScoreWeight *float64 `json:"scoreWeight"`
		} `json:"targets"`
	}
	if err := json.Unmarshal(head, &h); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(body, &meta); err != nil {
		t.Fatalf("/v1/meta: %v", err)
	}
	want, seen := h.Methodology.Settings.BrowserDifficulty, 0
	for _, mt := range meta.Targets {
		w, ok := want[mt.ID]
		switch {
		case ok && (mt.ScoreWeight == nil || *mt.ScoreWeight != w):
			t.Errorf("/v1/meta %s: scoreWeight %v, want %v", mt.ID, mt.ScoreWeight, w)
		case !ok && mt.ScoreWeight != nil:
			t.Errorf("/v1/meta %s: scoreWeight %v, want null", mt.ID, *mt.ScoreWeight)
		}
		if ok {
			seen++
		}
	}
	if seen != len(want) || len(want) != 9 {
		t.Errorf("/v1/meta: %d of %d weighted targets served, want 9", seen, len(want))
	}
}

// targetsScored checks every network of a targets answer has a ranked
// success score on each target and a ranked overall.
func targetsScored(t *testing.T, path string, body []byte) {
	t.Helper()
	var ans struct {
		Networks []struct {
			Slug           string                `json:"slug"`
			BrowserOverall servedOverall         `json:"browserOverall"`
			Cells          map[string]servedCell `json:"cells"`
		} `json:"networks"`
	}
	if err := json.Unmarshal(body, &ans); err != nil {
		t.Fatalf("%s: %v", path, err)
	}
	if len(ans.Networks) != len(browserScoresSuccess) {
		t.Fatalf("%s: %d networks, want %d", path, len(ans.Networks), len(browserScoresSuccess))
	}
	for _, n := range ans.Networks {
		want := browserScoresSuccess[n.Slug]
		for _, target := range browserScoresTargets {
			s := n.Cells[target].Scores
			if s == nil || !ranked(s.Success, want) {
				t.Errorf("%s: %s %s scores %+v, want a ranked %v", path, n.Slug, target, s, want)
			}
		}
		if o, ok := n.BrowserOverall["camoufox"]; !ok || !ranked(o.Success, want) {
			t.Errorf("%s: %s browserOverall %+v, want a ranked %v", path, n.Slug, n.BrowserOverall, want)
		}
	}
}

// profileScored checks a network page's browser targets and overall.
func profileScored(t *testing.T, slug string, want float64, body []byte) {
	t.Helper()
	var ans struct {
		Profile struct {
			BrowserOverall servedOverall `json:"browserOverall"`
			Targets        []struct {
				Target string        `json:"target"`
				Scores *servedScores `json:"scores"`
			} `json:"targets"`
		} `json:"profile"`
	}
	if err := json.Unmarshal(body, &ans); err != nil {
		t.Fatalf("%s: %v", slug, err)
	}
	scored := 0
	for _, target := range ans.Profile.Targets {
		if target.Scores != nil && ranked(target.Scores.Success, want) {
			scored++
		}
	}
	if scored != len(browserScoresTargets) {
		t.Errorf("%s: %d ranked target scores of %d, want %d", slug, scored, len(ans.Profile.Targets), len(browserScoresTargets))
	}
	if o, ok := ans.Profile.BrowserOverall["camoufox"]; !ok || !ranked(o.Success, want) {
		t.Errorf("%s: browserOverall %+v, want a ranked %v", slug, ans.Profile.BrowserOverall, want)
	}
}

func ranked(s servedScore, want float64) bool {
	return s.Status == "ranked" && s.Score != nil && *s.Score == want && s.RankedOf != nil && *s.RankedOf == 3
}
