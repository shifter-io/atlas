package build

import (
	"testing"

	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/snap"
)

// A window's challenge_overall maps to challengeScore (CHALLENGE-OVERALL-1),
// as success_overall to successScore; none maps to nil, also in the figures.
func TestChallengeScoreOfAWindow(t *testing.T) {
	w := snap.EmptyWindow()
	if s := challengeScore(w); s != nil {
		t.Errorf("challengeScore of an empty window %+v, want nil", s)
	}
	w.ChallengeOverall = &snap.SuccessOverall{Score: 6.5, Rank: 3, RankedOf: 9, Status: scoreRanked,
		Targets: map[string]Num{"generic_http": 6.5}, Weights: map[string]Num{"generic_http": 1}}
	s := challengeScore(w)
	if s == nil || s.Score != 6.5 || s.Rank != 3 || s.RankedOf != 9 || s.Targets["generic_http"] != 6.5 || s.Weights["generic_http"] != 1 {
		t.Errorf("challengeScore %+v, want 6.5, rank 3 of 9 with its target and weight", s)
	}
	if successScore(w) != nil {
		t.Error("a challenge overall made a success score")
	}
}

// A challengeScore's null score, rank and rankedOf have its status as their
// reason, as a successScore's; a null challengeScore the row's own reason.
func TestChallengeScoreNullsHaveTheirStatusAsReason(t *testing.T) {
	row := RankingRow{Requests: 10, Source: model.SourceMeasured, Breakdown: model.NewBreakdown()}
	row.ChallengeScore = &model.SuccessScore{MetricScore: model.MetricScore{Score: nan, Rank: nan, RankedOf: nan,
		Status: scoreNoRankedTargets}, Targets: map[string]Num{}, Weights: map[string]Num{}}
	if err := row.NAList.list(&row, nil, row.facts().code); err != nil {
		t.Fatal(err)
	}
	for _, p := range []string{"challengeScore.score", "challengeScore.rank", "challengeScore.rankedOf"} {
		if got := row.ReasonOf(p); got != scoreNoRankedTargets {
			t.Errorf("%s reason %q, want %s", p, got, scoreNoRankedTargets)
		}
	}
	row.ChallengeScore = nil
	if err := row.NAList.list(&row, nil, row.facts().code); err != nil {
		t.Fatal(err)
	}
	if got := row.ReasonOf("challengeScore"); got != NAInsufficient {
		t.Errorf("challengeScore reason %q, want %s", got, NAInsufficient)
	}
	if r := reasonFor("challengeScore.score"); r != insufficientSnap {
		t.Errorf("coverage reason of challengeScore.score %+v, want %+v", r, insufficientSnap)
	}
}

// The challenge overall is its own shift group: its best reads 10, the
// others keep their gap, and its target scores move with their targets'
// challenge scores; the success overall is unchanged by it.
func TestShiftSuccessScoresShiftsTheChallengeOverall(t *testing.T) {
	s := &snap.Snapshot{Products: []snap.Product{shiftProduct(8, 6, 9), shiftProduct(7, 5, 4)}}
	for i, v := range []Num{8.5, 6} {
		w := s.Products[i].Windows["30d"]
		w.ChallengeOverall = &snap.SuccessOverall{Score: v, Rank: Num(i + 1), RankedOf: 2, Status: scoreRanked, PreviousScore: nan,
			Targets: map[string]Num{"google_search": w.Targets["google_search"].Browser.Scores.Challenge.Score, "generic_http": 9},
			Weights: map[string]Num{"google_search": 4, "generic_http": 1}}
		s.Products[i].Windows["30d"] = w
	}
	shiftSuccessScores(s)
	w0, w1 := s.Products[0].Windows["30d"], s.Products[1].Windows["30d"]
	if w0.ChallengeOverall.Score != 10 || !near(w1.ChallengeOverall.Score, 7.5) {
		t.Errorf("challenge overalls %v, %v, want 10, 7.5", w0.ChallengeOverall.Score, w1.ChallengeOverall.Score)
	}
	if got := w1.ChallengeOverall.Targets["google_search"]; !near(got, w1.Targets["google_search"].Browser.Scores.Challenge.Score.F()) || !near(got, 9) {
		t.Errorf("challenge overall's target %v, want its target's shifted challenge score 9", got)
	}
	if w0.SuccessOverall.Score != 10 || !near(w1.SuccessOverall.Score, 9) {
		t.Errorf("success overalls %v, %v, want 10, 9", w0.SuccessOverall.Score, w1.SuccessOverall.Score)
	}
}

// attemptsPerSuccess is 1 ÷ the request success rate, null without one or at
// 0; at 0 its reason is no_successes, a more specific reason kept.
func TestAttemptsPerSuccess(t *testing.T) {
	if got := attemptsPerSuccess(0.8); !near(got, 1.25) {
		t.Errorf("attemptsPerSuccess(0.8) = %v, want 1.25", got)
	}
	for _, rate := range []Num{0, nan} {
		if got := attemptsPerSuccess(rate); got.OK() {
			t.Errorf("attemptsPerSuccess(%v) = %v, want null", rate, got)
		}
	}
	reason := func(rate Num, code string) string {
		// The time-slot rate is not attemptsPerSuccess's: only the request rate's 0 is no_successes.
		s := &RangeStats{WindowStats: &WindowStats{SuccessRate: 0.5, RequestSuccessRate: rate}}
		return s.altCode(func(string) string { return code })("attemptsPerSuccess")
	}
	cases := []struct {
		rate       Num
		code, want string
	}{{0, NAInsufficient, NANoSuccesses}, {nan, NAInsufficient, NAInsufficient}, {0, NANoRun, NANoRun}, {0, NANotMeasured, NANotMeasured}}
	for _, c := range cases {
		if got := reason(c.rate, c.code); got != c.want {
			t.Errorf("rate %v, code %s: reason %s, want %s", c.rate, c.code, got, c.want)
		}
	}
}
