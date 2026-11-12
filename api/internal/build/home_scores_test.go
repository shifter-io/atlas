package build

import (
	"encoding/json"
	"fmt"
	"os"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/config"
	"github.com/Zeus-Development/proxy-index/api/internal/model"
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
	if len(s.Values) != 3 || s.Values[0] != 9.3 || s.Values[1].OK() || s.Values[2] != 10 {
		t.Errorf("values %v", s.Values)
	}
	if model.SV(s.Status[0]) != scoreRanked || s.Status[1] != nil || model.SV(s.Status[2]) != scoreRanked {
		t.Errorf("status %v", s.Status)
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
	if len(s.Values) != homeNetworkSpark || len(s.Status) != homeNetworkSpark || s.Labels != 4 {
		t.Fatalf("%d values, %d statuses, labels %d", len(s.Values), len(s.Status), s.Labels)
	}
	if s.Values[0] != 10 || model.SV(s.Status[0]) != scoreRanked || s.Values[1].OK() || model.SV(s.Status[1]) != notEnoughData {
		t.Errorf("points %v %v", s.Values[:2], s.Status[:2])
	}
	if e := scoreSpark(nil, homeNetworkSpark, 0); len(e.Values) != 0 || len(e.Status) != 0 {
		t.Errorf("empty history: %+v", e)
	}
}
