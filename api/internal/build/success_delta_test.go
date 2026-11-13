package build

import (
	"encoding/json"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/Zeus-Development/proxy-index/api/internal/snap"
)

// windowOf decodes a snapshot window as the API reads one.
func windowOf(t *testing.T, body string) snap.Window {
	t.Helper()
	var w snap.Window
	if err := json.Unmarshal([]byte(body), &w); err != nil {
		t.Fatal(err)
	}
	return w
}

// SUCCESS-SCORE-DELTA-1: a delta is this build's score minus the previous
// build's, unrounded; null when either is null.
func TestScoreDeltaIsTheScoreMinusThePreviousOne(t *testing.T) {
	for _, c := range []struct {
		score, previous, want Num
	}{{7.25, 6, 1.25}, {4, 5.5, -1.5}, {6, 6, 0}, {nan, 6, nan}, {6, nan, nan}, {nan, nan, nan}} {
		if got := scoreDelta(c.score, c.previous); got.OK() != c.want.OK() || c.want.OK() && got != c.want {
			t.Errorf("scoreDelta(%v, %v) = %v, want %v", c.score, c.previous, got, c.want)
		}
	}
}

// A window's successScoreDelta reads success_overall.previous_score: set,
// null or absent (a snapshot from before it), and no success_overall at all.
func TestSuccessScoreDeltaOfAWindow(t *testing.T) {
	for _, c := range []struct {
		name, body string
		want       Num
	}{
		{"previous set", `{"success_overall":{"score":7.5,"status":"ranked","targets":{},"previous_score":7}}`, 0.5},
		{"previous null", `{"success_overall":{"score":7.5,"status":"ranked","previous_score":null}}`, nan},
		{"older snapshot", `{"success_overall":{"score":7.5,"status":"ranked","targets":["generic_http"]}}`, nan},
		{"current null", `{"success_overall":{"score":null,"status":"no_ranked_targets","previous_score":6}}`, nan},
		{"no overall", `{}`, nan},
	} {
		got := successScoreDelta(windowOf(t, c.body))
		if got.OK() != c.want.OK() || c.want.OK() && got != c.want {
			t.Errorf("%s: successScoreDelta %v, want %v", c.name, got, c.want)
		}
	}
	day := windowOf(t, `{"success_overall":{"score":9,"status":"ranked","previous_score":8}}`)
	month := windowOf(t, `{"success_overall":{"score":3,"status":"ranked"}}`)
	if p := performanceStats(month, day); p.SuccessScoreDelta24h != 1 || p.SuccessScoreDelta30d.OK() {
		t.Errorf("performance deltas %v and %v, want 1 and null", p.SuccessScoreDelta24h, p.SuccessScoreDelta30d)
	}
}

// A target's scores.success.delta reads its previous_score (an HTTP target's
// scores, a browser target's main engine copy); the main engine's own scores
// share it, the other engines' stay null.
func TestTargetScoresCarryTheirDelta(t *testing.T) {
	w := windowOf(t, `{"targets":{
		"generic_http":{"metrics":{},"scores":{"success":{"score":10,"rank":1,"ranked_of":3,"status":"ranked","previous_score":8.5}}},
		"generic_https":{"metrics":{},"scores":{"success":{"score":5.5,"rank":2,"ranked_of":3,"status":"ranked"}}},
		"amazon_pdp":{"metrics":{},"browser":{"engine":"camoufox",
			"scores":{"success":{"score":4,"status":"ranked","previous_score":6},"challenge":{"status":"ranked"},"block":{"status":"ranked"}},
			"engines":{"camoufox":{"completed":200,"scores":{"success":{"score":4,"status":"ranked"}}},
				"chrome":{"completed":200,"scores":{"success":{"score":9,"status":"ranked"}}}}}}}}`)
	if d := successScores(w.Targets["generic_http"].Scores).Success.Delta; d != 1.5 {
		t.Errorf("generic_http delta %v, want 1.5", d)
	}
	if d := successScores(w.Targets["generic_https"].Scores).Success.Delta; d.OK() {
		t.Errorf("generic_https delta %v, want null (no previous_score)", d)
	}
	b, scores := browserBenchmark(w, model.TargetMeta{ID: "amazon_pdp"}, nil)
	if scores == nil || scores.Success.Delta != -2 || b.Engines["camoufox"].Scores.Success.Delta != -2 {
		t.Errorf("amazon_pdp scores %+v, camoufox %+v; want delta -2 on both", scores, b.Engines["camoufox"].Scores)
	}
	if d := b.Engines["chrome"].Scores.Success.Delta; d.OK() {
		t.Errorf("chrome delta %v, want null", d)
	}
	body, err := json.Marshal(scores.Success)
	if err != nil {
		t.Fatal(err)
	}
	if string(body) != `{"score":4,"rank":null,"rankedOf":null,"status":"ranked","delta":-2}` {
		t.Errorf("scores.success %s", body)
	}
	var back model.TargetSuccess
	if err := json.Unmarshal([]byte(`{"score":4,"status":"ranked"}`), &back); err != nil || back.Delta.OK() || back.Score != 4 {
		t.Errorf("a stored score without delta reads %+v (%v), want a null delta", back, err)
	}
}

// A null delta has the reason no_score_to_compare in the page answers (after
// no_run_in_window) and its sentence in /v1/coverage.
func TestNullScoreDeltasHaveTheirReason(t *testing.T) {
	f := naFacts{measured: true, ran: true}
	for _, p := range []string{"successScoreDelta", "performance.successScoreDelta24h", "scores.success.delta",
		"targets[].scores.success.delta", "targets[].engines.chrome.scores.success.delta"} {
		if got := f.code(p); got != NANoScoreToCompare {
			t.Errorf("code(%s) = %q, want %s", p, got, NANoScoreToCompare)
		}
	}
	if got := (naFacts{measured: true}).code("successScoreDelta"); got != NANoRun {
		t.Errorf("without a run: %q, want %s", got, NANoRun)
	}
	if got := f.code("changelog[].delta"); got == NANoScoreToCompare {
		t.Errorf("another delta has %q", got)
	}
	for _, p := range []string{"performance.successScoreDelta24h", "performance.successScoreDelta30d",
		"productMeasurements.residential.targets[generic_http].scores.success.delta", "targets[amazon_pdp].engines.chrome.scores.success.delta"} {
		if got := reasonFor(p); got != noScoreToCompare {
			t.Errorf("reasonFor(%s) = %v", p, got)
		}
	}
	row := RankingRow{Requests: 10, Source: model.SourceMeasured, Breakdown: model.NewBreakdown()}
	row.WindowFigures = noFigures()
	if err := row.NAList.list(&row, nil, row.facts().code); err != nil {
		t.Fatal(err)
	}
	if got := row.ReasonOf("successScoreDelta"); got != NANoScoreToCompare {
		t.Errorf("ranking row successScoreDelta reason %q, want %s", got, NANoScoreToCompare)
	}
}
