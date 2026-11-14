package build

import (
	"encoding/json"
	"slices"
	"testing"

	"github.com/Zeus-Development/atlas/api/internal/model"
)

// checkTimes fails unless times has one entry per value and label, each the
// minutes its label is made from, oldest first down to the newest point (0
// at the build's as-of time at most).
func checkTimes(t *testing.T, name string, values int, labels []string, times []Num) {
	t.Helper()
	if len(times) != values || len(labels) != values {
		t.Errorf("%s: %d values, %d labels, %d times", name, values, len(labels), len(times))
		return
	}
	for i, m := range times {
		if !m.OK() || m < 0 || labels[i] != bucketLabel(m.F()) || i > 0 && m >= times[i-1] {
			t.Errorf("%s: point %d at %v minutes, label %q, after %v", name, i, m.F(), labels[i], times)
			return
		}
	}
}

// A spark's times are its points' minutes, thinned as its values.
func TestSparkSeriesTimesAreItsPointsMinutes(t *testing.T) {
	t.Parallel()
	points := []model.SeriesPoint{}
	for m := 1440; m >= 0; m -= 5 {
		points = append(points, model.SeriesPoint{T: Num(m), V: 0.9})
	}
	s := sparkSeries(points, 24)
	checkTimes(t, "spark", len(s.Values), s.Labels, s.Times)
	if s.Times[0] != 1440 || s.Times[23] != 0 || s.Labels[23] != "now" {
		t.Errorf("times %v, labels %v", s.Times, s.Labels)
	}
	if r := roundedSeries(s, 2); !slices.Equal(r.Times, s.Times) {
		t.Errorf("rounded times %v", r.Times)
	}
	if m := marketScoreSpark(s); !slices.Equal(m.Times, s.Times) || !slices.Equal(m.Labels, s.Labels) {
		t.Errorf("score spark times %v", m.Times)
	}
	if e := sparkSeries(nil, 24); len(e.Times) != 0 || e.Times == nil {
		t.Errorf("empty spark times %v", e.Times)
	}
}

// Every /v1/home series' points have their times: the shared times parallel
// the shared labels, every network curve is on the range's one grid (its
// own list, null at its gaps), the market's inline sparks carry their times
// and a network's score spark uses its success spark's.
func TestEveryHomeSeriesHasItsTimes(t *testing.T) {
	t.Parallel()
	pg, _ := homePages(t)
	for _, r := range Ranges {
		h := homeOf(t, pg, r.ID)
		if len(h.Times) != len(h.Labels) || len(h.Labels) == 0 {
			t.Fatalf("%s: %d times lists, %d label lists", r.ID, len(h.Times), len(h.Labels))
		}
		for i := range h.Times {
			checkTimes(t, r.ID+" times", len(h.Times[i]), h.Labels[i], h.Times[i])
		}
		curve := -1
		for _, n := range h.Networks {
			if len(n.Curve.Values) > 0 && curve < 0 {
				curve = n.Curve.Labels
			}
			if len(n.Curve.Values) > 0 && n.Curve.Labels != curve {
				t.Errorf("%s %s: curve on list %d, the first curve on %d", r.ID, n.Slug, n.Curve.Labels, curve)
			}
			for name, s := range map[string]HomeSeries{"curve": n.Curve, "success": n.SuccessSpark, "latency": n.LatencySpark} {
				if s.Labels < 0 || s.Labels >= len(h.Times) || len(h.Times[s.Labels]) != len(s.Values) {
					t.Errorf("%s %s %s: times %d of %d", r.ID, n.Slug, name, s.Labels, len(h.Times))
				}
			}
			if n.SuccessScoreSpark.Labels != n.SuccessSpark.Labels {
				t.Errorf("%s %s: score spark times %d, success spark %d", r.ID, n.Slug, n.SuccessScoreSpark.Labels, n.SuccessSpark.Labels)
			}
		}
		m := h.Market
		checkTimes(t, r.ID+" market curve", len(m.Curve.Values), m.Curve.Labels, m.Curve.Times)
		checkTimes(t, r.ID+" market successSpark", len(m.SuccessSpark.Values), m.SuccessSpark.Labels, m.SuccessSpark.Times)
		checkTimes(t, r.ID+" market latencySpark", len(m.LatencySpark.Values), m.LatencySpark.Labels, m.LatencySpark.Times)
		checkTimes(t, r.ID+" market successScoreSpark", len(m.SuccessScoreSpark.Values), m.SuccessScoreSpark.Labels, m.SuccessScoreSpark.Times)
	}
}

// Every /v1/home network series' timesIndex points to a times list of its
// length, the list its labels index names (SERIES-TIMES-INDEX-1).
func TestEveryHomeNetworkSeriesHasItsTimesIndex(t *testing.T) {
	t.Parallel()
	pg, _ := homePages(t)
	for _, r := range Ranges {
		h := homeOf(t, pg, r.ID)
		for _, n := range h.Networks {
			score := HomeSeries{Values: n.SuccessScoreSpark.Values, Labels: n.SuccessScoreSpark.Labels, TimesIndex: n.SuccessScoreSpark.TimesIndex}
			series := map[string]HomeSeries{"curve": n.Curve, "successSpark": n.SuccessSpark, "latencySpark": n.LatencySpark, "successScoreSpark": score}
			for name, s := range series {
				i := s.TimesIndex
				if i != s.Labels || i < 0 || i >= len(h.Times) || len(h.Times[i]) != len(s.Values) {
					t.Errorf("%s %s %s: timesIndex %d, labels %d, %d values, %d times lists", r.ID, n.Slug, name, i, s.Labels, len(s.Values), len(h.Times))
				}
			}
		}
	}
}

// The JSON names: a top-level times beside labels, times beside an inline
// spark's labels, timesIndex on every network series.
func TestHomeTimesFieldNames(t *testing.T) {
	t.Parallel()
	pg, _ := homePages(t)
	raw, err := json.Marshal(homeOf(t, pg, "24h"))
	if err != nil {
		t.Fatal(err)
	}
	var body struct {
		Times    [][]float64                  `json:"times"`
		Networks []map[string]json.RawMessage `json:"networks"`
		Market   struct {
			Curve, SuccessScoreSpark struct {
				Times []float64 `json:"times"`
			}
		} `json:"market"`
	}
	if err := json.Unmarshal(raw, &body); err != nil {
		t.Fatal(err)
	}
	if len(body.Times) == 0 || len(body.Market.Curve.Times) == 0 || len(body.Market.SuccessScoreSpark.Times) == 0 {
		t.Errorf("times %d lists, market curve %d, score spark %d", len(body.Times), len(body.Market.Curve.Times), len(body.Market.SuccessScoreSpark.Times))
	}
	if len(body.Networks) == 0 {
		t.Fatal("no networks")
	}
	for _, name := range []string{"curve", "successSpark", "latencySpark", "successScoreSpark"} {
		var s struct {
			TimesIndex *int `json:"timesIndex"`
		}
		if err := json.Unmarshal(body.Networks[0][name], &s); err != nil || s.TimesIndex == nil {
			t.Errorf("networks[0].%s: no timesIndex in %s (%v)", name, body.Networks[0][name], err)
		}
	}
}
