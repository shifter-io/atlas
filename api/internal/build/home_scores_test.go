package build

import (
	"slices"
	"testing"

	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/testscale"
)

// The market's spark is each point's rate on the 1-10 scale; a point
// without a rate is null with a null status.
func TestMarketScoreSparkIsAbsolute(t *testing.T) {
	s := marketScoreSpark(SparkSeries{Values: []Num{0.919, nan, 1}, Times: []Num{120, 60, 0}})
	if len(s.Times) != 3 || s.Times[2] != 0 || len(s.Values) != 3 || s.Values[0] != 9.3 || s.Values[1].OK() || s.Values[2] != 10 {
		t.Errorf("values %v", s.Values)
	}
	if model.SV(s.Statuses[0]) != scoreRanked || s.Statuses[1] != nil || model.SV(s.Statuses[2]) != scoreRanked {
		t.Errorf("status %v", s.Statuses)
	}
	for _, c := range []struct {
		rate  Num
		basis string
		score Num
	}{{0.5, FleetScoreBasis, 5.5}, {0.25, PassScoreBasis, 3.3}} {
		if m := absoluteScore(c.rate, c.basis); m.Score != c.score || model.SV(m.Status) != scoreRanked || m.Basis != c.basis {
			t.Errorf("absoluteScore(%v) = %+v", c.rate, m)
		}
	}
}

// The market's score delta is 9 × its history's change (closing fifth minus
// opening fifth), unobserved points left out; null below two points.
func TestMarketScoreDeltaIsNineTimesTheChange(t *testing.T) {
	history := []model.SeriesPoint{{T: 50, V: 0.8}, {T: 40, V: nan}, {T: 30, V: 0.85}, {T: 20, V: 0.85}, {T: 10, V: 0.85}, {T: 0, V: 0.9}}
	if d := marketScoreDelta(history); d != 0.9 {
		t.Errorf("delta %v, want 0.9 (9 × 10 points)", d)
	}
	if d := marketScoreDelta([]model.SeriesPoint{{T: 1, V: 0.9}, {T: 0, V: nan}}); d.OK() {
		t.Errorf("one observed point: %v, want null", d)
	}
}

// The market's most reliable network is the one with the highest success
// score, a tie going to the higher success rate; without any score, the
// highest success rate with a null score.
func TestMostReliableIsChosenByTheSuccessScore(t *testing.T) {
	stat := func(rate, score Num, status string) *WindowStats {
		s := &WindowStats{SuccessRate: rate}
		if status != "" {
			s.SuccessScore = &model.SuccessScore{MetricScore: model.MetricScore{Score: score, Status: status}}
		}
		return s
	}
	fleet, stats := []*entry{}, map[string]map[string]*WindowStats{}
	for slug, s := range map[string]*WindowStats{"a": stat(0.99, 7, scoreRanked), "b": stat(0.9, 10, scoreRanked),
		"c": stat(0.95, 10, scoreRanked), "d": stat(0.999, nan, notEnoughData)} {
		stats[slug] = map[string]*WindowStats{"7d": s}
	}
	for _, slug := range []string{"a", "b", "c", "d"} {
		fleet = append(fleet, &entry{P: &model.Provider{Slug: slug, Name: "N " + slug}})
	}
	byRate := StatPick{Provider: &ProviderRef{Slug: "d", Name: "N d"}, Stats: stats["d"]["7d"]}
	got := mostReliable(fleet, stats, "7d", byRate)
	if got == nil || got.Slug != "c" || got.Value != 0.95 || got.Score != 10 || model.SV(got.Status) != scoreRanked {
		t.Errorf("most reliable %+v, want c (10, the higher rate of the tie)", got)
	}
	got = mostReliable(fleet[3:], stats, "7d", byRate)
	if got == nil || got.Slug != "d" || got.Value != 0.999 || got.Score.OK() || model.SV(got.Status) != notEnoughData {
		t.Errorf("without a score %+v, want d by rate", got)
	}
	if got := mostReliable(nil, stats, "7d", StatPick{}); got != nil {
		t.Errorf("no fleet: %+v", got)
	}
}

// Every rankings row's and home network's score spark has one point per
// trend or success spark point, each with its status; the market's has the
// success spark's labels.
func TestScoreSparksFollowTheirSparks(t *testing.T) {
	t.Parallel()
	in, err := testscale.Inputs(testscale.Shape{Networks: 4, Countries: 1, Exits: 3, Tail: 4, SuccessScores: true})
	if err != nil {
		t.Fatal(err)
	}
	res, err := Build(in)
	if err != nil {
		t.Fatal(err)
	}
	ranked := 0
	for _, r := range Ranges {
		for _, row := range res.Pages.Rankings[r.ID].Networks {
			s := row.SuccessScoreSpark
			if len(s.Values) != len(row.Trend) || len(s.Statuses) != len(row.Trend) || len(row.Trend) == 0 {
				t.Errorf("%s %s: %d scores, %d statuses, %d trend points", r.ID, row.Slug, len(s.Values), len(s.Statuses), len(row.Trend))
			}
			for i, v := range s.Values {
				if s.Statuses[i] == nil || v.OK() != (model.SV(s.Statuses[i]) == scoreRanked) {
					t.Errorf("%s %s point %d: %v %v", r.ID, row.Slug, i, v, s.Statuses[i])
				}
				if st := model.SV(s.Statuses[i]); st == scoreRanked {
					ranked++ // the point was compared among 3 or more networks
				}
			}
		}
		home := res.Pages.Registered[homePath(r.ID)].(*HomeAnswer)
		for _, n := range home.Networks {
			if s := n.SuccessScoreSpark; len(s.Values) != len(n.SuccessSpark.Values) || len(s.Statuses) != len(s.Values) || s.TimesIndex != n.SuccessSpark.TimesIndex {
				t.Errorf("home %s %s: %d scores for %d points", r.ID, n.Slug, len(s.Values), len(n.SuccessSpark.Values))
			}
		}
		if m := home.Market; len(m.SuccessScoreSpark.Values) != len(m.SuccessSpark.Values) || !slices.Equal(m.SuccessScoreSpark.Times, m.SuccessSpark.Times) {
			t.Errorf("home %s market spark %+v", r.ID, m.SuccessScoreSpark)
		}
	}
	if ranked == 0 {
		t.Error("no spark point compared among the networks")
	}
}

// A score curve is each rate point on the 1-10 scale (1 + 9 × rate) to 2
// decimals, a gap kept null and the times kept (SUCCESS-CURVE-1, #1072).
func TestScoreCurveIsEachRateOnTheOneToTenScale(t *testing.T) {
	t.Parallel()
	times := []Num{40, 30, 20, 10, 0}
	got := scoreCurve(SparkSeries{Values: []Num{0.8677, nan, 0, 1, 0.5}, Times: times})
	want := []Num{8.81, nan, 1, 10, 5.5}
	if !slices.EqualFunc(got.Values, want, sameNum) || !slices.Equal(got.Times, times) {
		t.Errorf("score curve %v at %v, want %v", got.Values, got.Times, want)
	}
	if empty := scoreCurve(SparkSeries{Values: []Num{}, Times: []Num{}}); len(empty.Values) != 0 || empty.Times == nil {
		t.Errorf("empty curve %+v", empty)
	}
}
