package build

import (
	"encoding/json"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/snap"
)

// shiftProduct is a network of kind residential whose 30d window has the
// overall score, a browser target (camoufox) and an HTTP target scored, and
// an Index Score.
func shiftProduct(overall, target, http Num) snap.Product {
	w := snap.EmptyWindow()
	w.SuccessOverall = &snap.SuccessOverall{Score: overall, Rank: nan, RankedOf: nan, Status: scoreRanked, PreviousScore: overall - 0.5,
		Targets: map[string]Num{"google_search": target, "generic_http": http}, Weights: map[string]Num{"google_search": 4, "generic_http": 1}}
	scores := func(v Num) *snap.TargetScores {
		m := snap.MetricScore{Rate: 0.5, Score: v, Rank: 1, RankedOf: 3, Status: scoreRanked, PreviousScore: nan}
		return &snap.TargetScores{Success: m, Challenge: m, Block: m}
	}
	w.Targets = map[string]snap.TargetResult{
		"google_search": {Browser: &snap.BrowserResult{Engine: "camoufox", Scores: scores(target),
			Engines: map[string]snap.EngineResult{"camoufox": {Scores: scores(target)}}}},
		"generic_http": {Scores: scores(http)},
	}
	w.BrowserOverall = map[string]snap.BrowserOverall{"camoufox": {Success: snap.OverallScore{Score: target, Status: scoreRanked}}}
	grade := "A"
	w.Scores = map[string]snap.Score{"balanced": {Score: 71.5, Grade: &grade, Rank: 2, PreviousRank: 3, State: "ready"}}
	day := &snap.SuccessOverall{Score: target, Status: scoreRanked, PreviousScore: nan}
	return snap.Product{Kind: "residential", Windows: map[string]snap.Window{"30d": w},
		Days: []snap.Day{{Start: "2026-11-12T00:00:00Z", SuccessOverall: day}}}
}

// A group whose best is 8.05 shifts by 1.95, the best reading exactly 10 and
// the order and gaps holding; tied leaders at 6.03 read 10; each group has its
// own offset; deltas, nulls, statuses and the Index Score are unchanged.
func TestShiftSuccessScoresMakesEachGroupsBestTen(t *testing.T) {
	s := &snap.Snapshot{Products: []snap.Product{
		shiftProduct(8.05, 6.03, 9), shiftProduct(7.42, 6.03, 4), shiftProduct(nan, 3, nan),
	}}
	s.Products[2].Windows["30d"].SuccessOverall.Status = scoreNoRankedTargets
	other := shiftProduct(5, 5, 5)
	other.Kind = "mobile"
	s.Products = append(s.Products, other)
	index, err := json.Marshal(s.Products[0].Windows["30d"].Scores)
	if err != nil {
		t.Fatal(err)
	}
	shiftSuccessScores(s)
	w0, w1, w2 := s.Products[0].Windows["30d"], s.Products[1].Windows["30d"], s.Products[2].Windows["30d"]
	if w0.SuccessOverall.Score != 10 || !near(w1.SuccessOverall.Score, 7.42+1.95) {
		t.Errorf("overalls %v, %v, want 10, 9.37", w0.SuccessOverall.Score, w1.SuccessOverall.Score)
	}
	if !near(scoreDelta(w1.SuccessOverall.Score, w1.SuccessOverall.PreviousScore), 0.5) {
		t.Errorf("delta %v, want the measured 0.5", scoreDelta(w1.SuccessOverall.Score, w1.SuccessOverall.PreviousScore))
	}
	if w2.SuccessOverall.Score.OK() || w2.SuccessOverall.Status != scoreNoRankedTargets {
		t.Errorf("a null overall %+v, want null with its status", w2.SuccessOverall)
	}
	for i, w := range []snap.Window{w0, w1} {
		b := w.Targets["google_search"].Browser
		if b.Scores.Success.Score != 10 || b.Engines["camoufox"].Scores.Block.Score != 10 || w.BrowserOverall["camoufox"].Success.Score != 10 ||
			w.SuccessOverall.Targets["google_search"] != 10 || s.Products[i].Days[0].SuccessOverall.Score != 10 {
			t.Errorf("network %d: tied leaders at 6.03 %+v, want 10 everywhere", i, b.Scores)
		}
	}
	if got := w2.Targets["google_search"].Browser.Scores.Challenge.Score; !near(got, 3+3.97) {
		t.Errorf("a trailing target score %v, want its gap kept: 6.97", got)
	}
	if http := w1.Targets["generic_http"].Scores.Success; !near(http.Score, 5) || http.Rank != 1 || http.Status != scoreRanked {
		t.Errorf("HTTP target %+v, want 4 + 1 (its own offset), rank and status kept", http)
	}
	if !near(w1.SuccessOverall.Targets["generic_http"], 5) || w2.Targets["generic_http"].Scores.Success.Score.OK() {
		t.Error("the overall's HTTP target score is not shifted with its target, or a null moved")
	}
	if mobile := s.Products[3].Windows["30d"].SuccessOverall.Score; mobile != 10 {
		t.Errorf("the only mobile network %v, want 10 (its own group)", mobile)
	}
	after, err := json.Marshal(w0.Scores)
	if err != nil {
		t.Fatal(err)
	}
	if string(after) != string(index) {
		t.Errorf("Index Score %s, want byte-equal %s", after, index)
	}
	shiftSuccessScores(nil)
}

// An all-null group is unchanged.
func TestShiftSuccessScoresLeavesAnAllNullGroup(t *testing.T) {
	p := shiftProduct(nan, nan, nan)
	s := &snap.Snapshot{Products: []snap.Product{p}}
	shiftSuccessScores(s)
	w := s.Products[0].Windows["30d"]
	if w.SuccessOverall.Score.OK() || w.SuccessOverall.PreviousScore.OK() || w.Targets["generic_http"].Scores.Success.Score.OK() {
		t.Errorf("an all-null group moved: %+v", w.SuccessOverall)
	}
}

// The scores the build ranks itself read 10 at the top: tied leaders (16 of
// 18 at the average rank 8.5, 6.03 unshifted) and the rest keep their gaps;
// the absolute scores are not shifted.
func TestRankedScoresPutTiedLeadersAtTen(t *testing.T) {
	rated := make([]ratedRate, 18)
	for i := range rated {
		rated[i] = ratedRate{0.9, 1000}
	}
	rated[16], rated[17] = ratedRate{0.5, 1000}, ratedRate{0.2, 1000}
	got := metricScores(rated)
	if got[0].Score != 10 || got[15].Score != 10 || got[0].Rank != 8.5 || got[0].Status != scoreRanked {
		t.Errorf("tied leaders %+v, want 10 at rank 8.5", got[0])
	}
	// Unshifted: 6.03 for the leaders, 1.53 and 1 below; gaps 4.5 and 0.53.
	if !near(got[16].Score, (10-(rankScore(8.5, 18)-rankScore(17, 18))).F()) || !near(got[17].Score, (10-(rankScore(8.5, 18)-1)).F()) {
		t.Errorf("trailing scores %v, %v, want their measured gaps kept", got[16].Score, got[17].Score)
	}
	if v := absoluteValue(0.5); v != 5.5 {
		t.Errorf("absolute score %v, want 5.5 unshifted", v)
	}
	pg := &Pages{standings: map[placeScoreKey]placeStanding{{"30d", "country", "residential", "DE"}: {status: scoreRanked,
		ranks: []placeRank{{"a", 1.5}, {"b", 1.5}, {"c", 3}}}}}
	row := PlaceRow{Key: "DE", SuccessRate: 0.5, Requests: 500}
	if a, c := pg.placeScore("a", "residential", "30d", "country", row), pg.placeScore("c", "residential", "30d", "country", row); a.Score != 10 ||
		!near(c.Score, (10-(rankScore(1.5, 3)-1)).F()) || a.Rank != 1.5 {
		t.Errorf("place scores %+v, %+v, want 10 and the measured gap", a, c)
	}
}
