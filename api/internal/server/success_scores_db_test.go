package server

import (
	"encoding/json"
	"os"
	"strings"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/build"
	"github.com/Zeus-Development/proxy-index/api/internal/config"
	"github.com/Zeus-Development/proxy-index/api/internal/testdb"
	"github.com/Zeus-Development/proxy-index/api/internal/testscale"
)

// successScoreTargets are the network targets SUCCESS-SCORE-CALC-1 scores.
var successScoreTargets = []string{"generic_http", "generic_https"}

// previousScores are the success scores of the build before the scored one
// (SUCCESS-SCORE-DELTA-1): alpha's every score was 8.5 (so its deltas are
// 1.5), beta's null (no previous build), gamma's absent (an older snapshot).
var previousScores = map[string]any{"alpha": 8.5, "beta": nil}

// withSuccessScores is the browser scores head with SUCCESS-SCORE-CALC-1's
// keys in every network window, as the calculator writes them: each network
// target's scores.success and success_overall, alpha 10, beta 5.5, gamma 1,
// each with its previousScores (a browser target's main engine copy too).
func withSuccessScores(t *testing.T, head []byte) []byte {
	t.Helper()
	var doc map[string]any
	if err := json.Unmarshal(head, &doc); err != nil {
		t.Fatal(err)
	}
	rank := map[string]float64{"alpha": 1, "beta": 2, "gamma": 3}
	for _, n := range doc["networks"].([]any) {
		n := n.(map[string]any)
		r := rank[n["provider"].(string)]
		score := 1 + 9*(3-r)/2
		previous, has := previousScores[n["provider"].(string)]
		for _, w := range n["windows"].(map[string]any) {
			w := w.(map[string]any)
			targets := w["targets"].(map[string]any)
			scores, weights := map[string]any{}, map[string]any{}
			for _, id := range successScoreTargets {
				success := map[string]any{"rate": 0.9, "score": score, "rank": r, "ranked_of": 3, "status": "ranked"}
				targets[id].(map[string]any)["scores"] = map[string]any{"success": success}
				scores[id], weights[id] = score, 1
				if has {
					success["previous_score"] = previous
				}
			}
			w["success_overall"] = map[string]any{"score": score, "rank": r, "ranked_of": 3, "status": "ranked",
				"targets": scores, "weights": weights}
			if has {
				w["success_overall"].(map[string]any)["previous_score"] = previous
				for _, id := range browserScoresTargets {
					b := targets[id].(map[string]any)["browser"].(map[string]any)
					b["scores"].(map[string]any)["success"].(map[string]any)["previous_score"] = previous
				}
			}
		}
	}
	out, err := json.Marshal(doc)
	if err != nil {
		t.Fatal(err)
	}
	return out
}

func successScoresAnswers(t *testing.T, scored bool) map[string][]byte {
	t.Helper()
	head, err := os.ReadFile(config.FindUp(browserScoresHead))
	if err != nil {
		t.Fatal(err)
	}
	if scored {
		head = withSuccessScores(t, head)
	}
	db := testdb.New(t)
	db.Exec(t, testscale.BrowserTargetsSQL())
	return renderedAnswers(t, db, db.Publish(t, head), nil, func(*build.Result) {})
}

// successScorePaths are, per answer, the JSON paths of every network row's
// successScore; "*" is each element of a list or each range of a map.
var successScorePaths = map[string][]string{
	"/v1/rankings/7d":              {"networks.*.successScore"},
	"/v1/rankings/30d/residential": {"networks.*.successScore"},
	"/v1/overlap/residential/30d":  {"networks.*.successScore"},
	"/v1/networks":                 {"networks.*.successScore"},
	"/v1/networks/beta/residential": {"network.successScore", "profile.successScore", "ranges.*.successScore",
		"stats.*.successScore"},
}

// SUCCESS-SCORE-API-1: the calculator's success scores are served on every
// network row, on each network target's cells with its shared status, and
// the market's score and the score definitions beside them.
func TestTheCalculatorsSuccessScoresAreServed(t *testing.T) {
	t.Parallel()
	answers := successScoresAnswers(t, true)
	for path, fields := range successScorePaths {
		for _, field := range fields {
			for _, v := range valuesAt(t, path, answers[path], field) {
				s, _ := v.(map[string]any)
				targets, _ := s["targets"].(map[string]any)
				if s["status"] != "ranked" || s["rankedOf"] != 3.0 || s["score"] == nil || len(targets) != 2 {
					t.Errorf("%s %s = %v, want a ranked score of 3 over 2 targets", path, field, v)
				}
			}
		}
	}
	// The home's rows carry the compact score, without targets and weights (HOME-SIZE-1).
	for _, v := range valuesAt(t, "/v1/home/24h", answers["/v1/home/24h"], "networks.*.successScore") {
		if s, _ := v.(map[string]any); s["status"] != "ranked" || s["rankedOf"] != 3.0 || s["score"] == nil || s["targets"] != nil || s["weights"] != nil {
			t.Errorf("/v1/home/24h successScore %v, want ranked of 3 without targets and weights", v)
		}
	}
	for _, path := range []string{"/v1/targets/residential/24h", "/v1/targets/residential/30d/camoufox"} {
		for _, v := range valuesAt(t, path, answers[path], "networks.*.successScore") {
			if s, _ := v.(map[string]any); s["status"] != "ranked" || s["rankedOf"] != 3.0 || s["score"] == nil || s["targets"] != nil {
				t.Errorf("%s successScore %v, want ranked of 3 without targets", path, v)
			}
		}
		for _, id := range successScoreTargets {
			for _, v := range valuesAt(t, path, answers[path], "networks.*.cells."+id+".scores.success") {
				if s, _ := v.(map[string]any); s["status"] != "ranked" || s["rankedOf"] != 3.0 {
					t.Errorf("%s %s cell score %v, want ranked of 3", path, id, v)
				}
			}
		}
		for _, v := range valuesAt(t, path, answers[path], "targets.*") {
			te, _ := v.(map[string]any)
			if id := te["id"]; id == "generic_http" || id == "generic_https" {
				status, _ := te["scoreStatus"].(map[string]any)
				if http, _ := status["http"].(map[string]any); http["status"] != "ranked" {
					t.Errorf("%s %s scoreStatus %v, want http ranked", path, id, te["scoreStatus"])
				}
			}
		}
	}
	var all struct {
		NA      []string          `json:"na"`
		Reason  string            `json:"naReason"`
		Reasons map[string]string `json:"naReasons"`
	}
	if err := json.Unmarshal(answers["/v1/networks/all/all"], &all); err != nil {
		t.Fatal(err)
	}
	for _, p := range []string{"network.successScore", "ranges.30d.successScore"} {
		if r, ok := all.Reasons[p]; !ok && all.Reason != "not_applicable" || ok && r != "not_applicable" || !strings.Contains(strings.Join(all.NA, ","), p) {
			t.Errorf("all networks %s: na %v reason %q reasons %v, want not_applicable", p, all.NA, all.Reason, all.Reasons)
		}
	}
	market, _ := valuesAt(t, "/v1/home/24h", answers["/v1/home/24h"], "market")[0].(map[string]any)
	if s, _ := market["successScore"].(map[string]any); s["basis"] != "median_success_rate" || s["status"] != "ranked" || s["score"] == nil {
		t.Errorf("market.successScore %v, want a ranked score on the median success rate", s)
	}
}

// successDeltaPaths are, per answer, the JSON paths of every network's
// successScoreDelta and of its targets' scores.success.delta; "<slug>" is
// the network's row or profile.
var successDeltaPaths = map[string][]string{
	"/v1/rankings/7d":             {"networks.<slug>.successScoreDelta"},
	"/v1/home/24h":                {"networks.<slug>.successScoreDelta"},
	"/v1/targets/residential/24h": {"networks.<slug>.cells.generic_http.scores.success.delta", "networks.<slug>.cells.amazon_pdp.scores.success.delta"},
	"/v1/networks/<slug>/residential": {"stats.24h.successScoreDelta", "stats.30d.successScoreDelta",
		"profile.performance.successScoreDelta24h", "profile.performance.successScoreDelta30d", "profile.targets.*.scores.success.delta"},
}

// SUCCESS-SCORE-DELTA-1: a network's successScoreDelta and its targets'
// scores.success.delta are its scores minus the previous build's (alpha:
// 10 − 8.5), null with no_score_to_compare when the previous build has none
// (beta) or the snapshot predates previous scores (gamma).
func TestSuccessScoreDeltasAreServed(t *testing.T) {
	t.Parallel()
	answers := successScoresAnswers(t, true)
	want := map[string]any{"alpha": 1.5, "beta": nil, "gamma": nil}
	for path, fields := range successDeltaPaths {
		for slug, delta := range want {
			at := strings.ReplaceAll(path, "<slug>", slug)
			body := answers[at]
			for _, field := range fields {
				got := deltasOf(t, at, body, field, slug)
				for _, v := range got {
					if v != delta {
						t.Errorf("%s %s %s = %v, want %v", at, slug, field, v, delta)
					}
				}
				if len(got) == 0 {
					t.Errorf("%s %s: no %s", at, slug, field)
				}
			}
		}
	}
	var row struct {
		Networks []struct {
			Slug    string            `json:"slug"`
			Reason  string            `json:"naReason"`
			Reasons map[string]string `json:"naReasons"`
		} `json:"networks"`
	}
	if err := json.Unmarshal(answers["/v1/rankings/7d"], &row); err != nil {
		t.Fatal(err)
	}
	for _, n := range row.Networks {
		if r, ok := n.Reasons["successScoreDelta"]; n.Slug != "alpha" && (ok && r != "no_score_to_compare" || !ok && n.Reason != "no_score_to_compare") {
			t.Errorf("rankings %s successScoreDelta reason %q (row %q), want no_score_to_compare", n.Slug, r, n.Reason)
		}
	}
}

// deltasOf is every value at field of body; a "networks.<slug>." field is
// that network's row only.
func deltasOf(t *testing.T, path string, body []byte, field, slug string) []any {
	t.Helper()
	rest, ok := strings.CutPrefix(field, "networks.<slug>.")
	if !ok {
		return valuesAt(t, path, body, field)
	}
	out := []any{}
	for _, v := range valuesAt(t, path, body, "networks.*") {
		if row, _ := v.(map[string]any); row["slug"] == slug {
			b, err := json.Marshal(row)
			if err != nil {
				t.Fatal(err)
			}
			out = append(out, valuesAt(t, path, b, rest)...)
		}
	}
	return out
}

// An older build without the success scores serves them null, each with a
// reason in the row's na.
func TestSuccessScoresOfAnOlderBuildAreNullWithAReason(t *testing.T) {
	t.Parallel()
	answers := successScoresAnswers(t, false)
	for _, path := range []string{"/v1/rankings/7d", "/v1/home/24h", "/v1/overlap/residential/30d"} {
		for _, v := range valuesAt(t, path, answers[path], "networks.*") {
			row, _ := v.(map[string]any)
			if row["successScore"] != nil || !strings.Contains(strings.Join(naOfRow(row), ","), "successScore") {
				t.Errorf("%s %s: successScore %v, na %v; want null and listed", path, row["slug"], row["successScore"], row["na"])
			}
		}
	}
	for _, v := range valuesAt(t, "/v1/targets/residential/24h", answers["/v1/targets/residential/24h"], "networks.*.cells.generic_http") {
		if cell, _ := v.(map[string]any); cell["scores"] != nil {
			t.Errorf("generic_http cell %v, want null scores", v)
		}
	}
	var targets struct {
		NA []string `json:"na"`
	}
	if err := json.Unmarshal(answers["/v1/targets/residential/24h"], &targets); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(strings.Join(targets.NA, ","), "networks[].cells.generic_http.scores") {
		t.Errorf("targets na %v, want the generic_http cells' scores", targets.NA)
	}
}

func naOfRow(row map[string]any) []string {
	out := []string{}
	na, _ := row["na"].([]any)
	for _, p := range na {
		s, _ := p.(string)
		out = append(out, s)
	}
	return out
}

// valuesAt is every value at a dotted path of an answer ("*": every element
// or member); it fails the test when the answer or a step is missing.
func valuesAt(t *testing.T, path string, body []byte, field string) []any {
	t.Helper()
	if body == nil {
		t.Fatalf("no answer %s", path)
	}
	var doc any
	if err := json.Unmarshal(body, &doc); err != nil {
		t.Fatalf("%s: %v", path, err)
	}
	values := []any{doc}
	for _, step := range strings.Split(field, ".") {
		next := []any{}
		for _, v := range values {
			switch x := v.(type) {
			case []any:
				next = append(next, x...)
			case map[string]any:
				if step == "*" {
					for _, m := range x {
						next = append(next, m)
					}
				} else if m, ok := x[step]; ok {
					next = append(next, m)
				} else {
					t.Fatalf("%s: no %s in %s", path, step, field)
				}
			}
		}
		values = next
	}
	if len(values) == 0 {
		t.Fatalf("%s: nothing at %s", path, field)
	}
	return values
}
