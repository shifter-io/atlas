package build

import (
	"encoding/json"
	"math"
	"os"
	"testing"

	"github.com/shifter-io/atlas/api/internal/config"
	"github.com/shifter-io/atlas/api/internal/model"
	"github.com/shifter-io/atlas/api/internal/snap"
)

// scoreCases is the rank rule's cases tests/test_success_score_cases.py
// checks atlas's metric_scores against.
const scoreCases = "fixtures/scores/success-score-cases.json"

type scoreCase struct {
	Name  string       `json:"name"`
	Rated [][2]float64 `json:"rated"`
	Want  []struct {
		Score    *float64 `json:"score"`
		Rank     *float64 `json:"rank"`
		RankedOf *float64 `json:"ranked_of"`
		Status   string   `json:"status"`
	} `json:"want"`
}

func sameScore(got Num, want *float64) bool {
	if want == nil {
		return !got.OK()
	}
	return got.OK() && got.F() == *want
}

// caseOffset is what a case's scores gain so its best reads 10 (SUCCESS-SHIFT-1):
// the calculator's cases are the measured, unshifted scores.
func caseOffset(c scoreCase) float64 {
	best := math.Inf(-1)
	for _, w := range c.Want {
		if w.Score != nil {
			best = max(best, *w.Score)
		}
	}
	return bestShownScore - best
}

func sameShiftedScore(got Num, want *float64, offset float64) bool {
	if want == nil {
		return !got.OK()
	}
	return got.OK() && math.Abs(got.F()-(*want+offset)) < 1e-9
}

// A place's rows score their networks as atlas scores a target's: the
// same cases give the same ranks, statuses, ties and minimums, and the same
// scores shifted so the best reads 10, the rows of each kind ranked apart.
func TestPlaceScoresFollowTheCalculatorsRule(t *testing.T) {
	data, err := os.ReadFile(config.FindUp(scoreCases))
	if err != nil {
		t.Fatal(err)
	}
	var doc struct {
		Cases []scoreCase `json:"cases"`
	}
	if err := json.Unmarshal(data, &doc); err != nil {
		t.Fatal(err)
	}
	for _, c := range doc.Cases {
		rows := []PlaceRow{}
		for _, r := range c.Rated {
			rows = append(rows, PlaceRow{Kind: "residential", SuccessRate: Num(r[0]), Requests: Num(r[1])},
				PlaceRow{Kind: "datacenter", SuccessRate: nan, Requests: nan})
		}
		placeScores(rows)
		offset := caseOffset(c)
		for i, want := range c.Want {
			got := rows[2*i].SuccessScore
			if got == nil || got.Status != want.Status || !sameShiftedScore(got.Score, want.Score, offset) || !sameScore(got.Rank, want.Rank) ||
				!sameScore(got.RankedOf, want.RankedOf) {
				t.Errorf("%s: row %d %+v, want %+v", c.Name, i, got, want)
			}
			if other := rows[2*i+1].SuccessScore; other == nil || other.Status != notEnoughData {
				t.Errorf("%s: datacenter row %+v, want not_enough_data", c.Name, other)
			}
		}
	}
}

// The market score is the median success rate on the 1-10 scale, absolute.
func TestMarketScoreIsTheMedianSuccessRateOnTenPoints(t *testing.T) {
	for _, c := range []struct {
		median Num
		score  Num
	}{{0.919, 9.3}, {1, 10}, {0, 1}, {0.5, 5.5}} {
		m := marketScore(c.median)
		if m.Score != c.score || model.SV(m.Status) != "ranked" || m.Basis != "median_success_rate" {
			t.Errorf("marketScore(%v) = %+v, want %v ranked", c.median, m, c.score)
		}
	}
	if m := marketScore(nan); m.Score.OK() || m.Status != nil || m.Basis != "median_success_rate" {
		t.Errorf("marketScore(NaN) = %+v, want a null score and status", m)
	}
}

// A successScore's null score, rank and rankedOf have its status as their
// reason; a null successScore the row's own reason.
func TestSuccessScoreNullsHaveTheirStatusAsReason(t *testing.T) {
	row := RankingRow{Requests: 10, Source: model.SourceMeasured, Breakdown: model.NewBreakdown()}
	row.SuccessScore = &model.SuccessScore{MetricScore: model.MetricScore{Score: nan, Rank: nan, RankedOf: nan,
		Status: scoreNoRankedTargets}, Targets: map[string]Num{}, Weights: map[string]Num{}}
	if err := row.NAList.list(&row, nil, row.facts().code); err != nil {
		t.Fatal(err)
	}
	for _, p := range []string{"successScore.score", "successScore.rank", "successScore.rankedOf"} {
		if got := row.ReasonOf(p); got != scoreNoRankedTargets {
			t.Errorf("%s reason %q, want %s", p, got, scoreNoRankedTargets)
		}
	}
	row.SuccessScore = nil
	if err := row.NAList.list(&row, nil, row.facts().code); err != nil {
		t.Fatal(err)
	}
	if got := row.ReasonOf("successScore"); got != NAInsufficient {
		t.Errorf("successScore reason %q, want %s", got, NAInsufficient)
	}
}

// A window's success_overall maps to successScore (targets and weights never
// null); none maps to nil. A network target's scores are success only.
func TestSuccessScoreOfAWindow(t *testing.T) {
	if s := successScore(snap.EmptyWindow()); s != nil {
		t.Errorf("successScore of an empty window %+v, want nil", s)
	}
	w := snap.EmptyWindow()
	w.SuccessOverall = &snap.SuccessOverall{Score: 7, Rank: 2, RankedOf: 4, Status: scoreRanked}
	s := successScore(w)
	if s == nil || s.Score != 7 || s.Rank != 2 || s.RankedOf != 4 || s.Targets == nil || s.Weights == nil {
		t.Errorf("successScore %+v, want 7, rank 2 of 4 with empty targets and weights", s)
	}
	scores := successScores(&snap.TargetScores{Success: snap.MetricScore{Score: 10, Rank: 1, RankedOf: 3, Status: scoreRanked,
		PreviousScore: nan}})
	body, err := json.Marshal(scores)
	if err != nil {
		t.Fatal(err)
	}
	if string(body) != `{"success":{"score":10,"rank":1,"rankedOf":3,"status":"ranked","delta":null}}` {
		t.Errorf("network target scores %s, want success only", body)
	}
	if successScores(nil) != nil {
		t.Error("successScores(nil) is not nil")
	}
}
