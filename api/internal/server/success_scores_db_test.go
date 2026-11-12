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

// withSuccessScores is the browser scores head with SUCCESS-SCORE-CALC-1's
// keys in every network window, as the calculator writes them: each network
// target's scores.success and success_overall, alpha 10, beta 5.5, gamma 1.
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
		for _, w := range n["windows"].(map[string]any) {
			w := w.(map[string]any)
			targets := w["targets"].(map[string]any)
			scores, weights := map[string]any{}, map[string]any{}
			for _, id := range successScoreTargets {
				targets[id].(map[string]any)["scores"] = map[string]any{"success": map[string]any{"rate": 0.9, "score": score,
					"rank": r, "ranked_of": 3, "status": "ranked"}}
				scores[id], weights[id] = score, 1
			}
			w["success_overall"] = map[string]any{"score": score, "rank": r, "ranked_of": 3, "status": "ranked",
				"targets": scores, "weights": weights}
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
	"/v1/home/24h":                 {"networks.*.successScore"},
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
	for _, path := range []string{"/v1/meta", "/v1/home/30d", "/v1/rankings/24h"} {
		d, _ := valuesAt(t, path, answers[path], "scoreDefinitions")[0].(map[string]any)
		statuses, _ := d["status"].(map[string]any)
		if d["rank"] == nil || d["market"] == nil || len(statuses) != 5 {
			t.Errorf("%s scoreDefinitions %v, want rank, market and 5 statuses", path, d)
		}
	}
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
