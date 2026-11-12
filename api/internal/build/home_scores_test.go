package build

import (
	"encoding/json"
	"fmt"
	"os"
	"slices"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/config"
	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/Zeus-Development/proxy-index/api/internal/testscale"
)

// A spark point ranks the networks with a point at that time as proxy-index
// ranks a target's: each of the shared cases as one point gives the same
// scores, statuses, ties and minimums; a point only two networks have is
// too_few_networks.
func TestPointScoresFollowTheCalculatorsRule(t *testing.T) {
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
	series := map[string][]ratedPoint{}
	for at, c := range doc.Cases {
		for i, r := range c.Rated {
			key := fmt.Sprintf("net%02d", i)
			series[key] = append(series[key], ratedPoint{at: int64(at), rate: ratedRate{Num(r[0]), Num(r[1])}})
		}
	}
	lone := int64(len(doc.Cases))
	series["net00"] = append(series["net00"], ratedPoint{at: lone, rate: ratedRate{0.9, 1000}})
	series["net01"] = append(series["net01"], ratedPoint{at: lone, rate: ratedRate{0.5, 1000}})
	got := pointScores(series)
	seen := map[string]int{}
	for _, c := range doc.Cases {
		for i, want := range c.Want {
			key := fmt.Sprintf("net%02d", i)
			s := got[key][seen[key]]
			seen[key]++
			if s.Status != want.Status || !sameScore(s.Score, want.Score) || !sameScore(s.Rank, want.Rank) || !sameScore(s.RankedOf, want.RankedOf) {
				t.Errorf("%s: network %d %+v, want %+v", c.Name, i, s, want)
			}
		}
	}
	for _, key := range []string{"net00", "net01"} {
		if s := got[key][len(got[key])-1]; s.Status != scoreTooFewNetworks || s.Score.OK() {
			t.Errorf("%s at the two-network point: %+v, want too_few_networks", key, s)
		}
	}
}

// The market's spark is each point's rate on the 1-10 scale; a point
// without a rate is null with a null status.
func TestMarketScoreSparkIsAbsolute(t *testing.T) {
	s := marketScoreSpark(SparkSeries{Values: []Num{0.919, nan, 1}, Labels: []string{"a", "b", "c"}})
	if len(s.Labels) != 3 || s.Labels[2] != "c" || len(s.Values) != 3 || s.Values[0] != 9.3 || s.Values[1].OK() || s.Values[2] != 10 {
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

// A network's score spark is thinned as its success spark, with its labels;
// a history without request counts is not_enough_data at every point.
func TestScoreSparkThinsAsTheSuccessSpark(t *testing.T) {
	scores := unrated(30)
	scores[0] = model.MetricScore{Score: 10, Rank: 1, RankedOf: 3, Status: scoreRanked}
	s := scoreSpark(scores, homeNetworkSpark, 4)
	if len(s.Values) != homeNetworkSpark || len(s.Statuses) != homeNetworkSpark || s.Labels != 4 {
		t.Fatalf("%d values, %d statuses, labels %d", len(s.Values), len(s.Statuses), s.Labels)
	}
	if s.Values[0] != 10 || model.SV(s.Statuses[0]) != scoreRanked || s.Values[1].OK() || model.SV(s.Statuses[1]) != notEnoughData {
		t.Errorf("points %v %v", s.Values[:2], s.Statuses[:2])
	}
	if e := scoreSpark(nil, homeNetworkSpark, 0); len(e.Values) != 0 || len(e.Statuses) != 0 {
		t.Errorf("empty history: %+v", e)
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
			if s := n.SuccessScoreSpark; len(s.Values) != len(n.SuccessSpark.Values) || len(s.Statuses) != len(s.Values) || s.Labels != n.SuccessSpark.Labels {
				t.Errorf("home %s %s: %d scores for %d points", r.ID, n.Slug, len(s.Values), len(n.SuccessSpark.Values))
			}
		}
		if m := home.Market; len(m.SuccessScoreSpark.Values) != len(m.SuccessSpark.Values) || !slices.Equal(m.SuccessScoreSpark.Labels, m.SuccessSpark.Labels) {
			t.Errorf("home %s market spark %+v", r.ID, m.SuccessScoreSpark)
		}
	}
	if ranked == 0 {
		t.Error("no spark point compared among the networks")
	}
}
