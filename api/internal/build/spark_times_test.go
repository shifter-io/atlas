package build

import (
	"bytes"
	"encoding/json"
	"slices"
	"testing"

	"github.com/Zeus-Development/atlas/api/internal/model"
)

// checkTimes fails unless times has one entry per value, each in minutes,
// oldest first down to the newest point (0 at the build's as-of time at
// most).
func checkTimes(t *testing.T, name string, values int, times []Num) {
	t.Helper()
	if len(times) != values {
		t.Errorf("%s: %d values, %d times", name, values, len(times))
		return
	}
	for i, m := range times {
		if !m.OK() || m < 0 || i > 0 && m >= times[i-1] {
			t.Errorf("%s: point %d at %v minutes, after %v", name, i, m.F(), times)
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
	checkTimes(t, "spark", len(s.Values), s.Times)
	if s.Times[0] != 1440 || s.Times[23] != 0 {
		t.Errorf("times %v", s.Times)
	}
	if r := roundedSeries(s, 2); !slices.Equal(r.Times, s.Times) {
		t.Errorf("rounded times %v", r.Times)
	}
	if m := marketScoreSpark(s); !slices.Equal(m.Times, s.Times) {
		t.Errorf("score spark times %v", m.Times)
	}
	if e := sparkSeries(nil, 24); len(e.Times) != 0 || e.Times == nil {
		t.Errorf("empty spark times %v", e.Times)
	}
}

// Every /v1/home series' points have their times: each shared times list is
// in order, every network score curve is on the range's one grid (its own list,
// null at its gaps), the market's inline sparks carry their times and a
// network's score spark uses its success spark's.
func TestEveryHomeSeriesHasItsTimes(t *testing.T) {
	t.Parallel()
	pg, _ := homePages(t)
	for _, r := range Ranges {
		h := homeOf(t, pg, r.ID)
		if len(h.Times) == 0 {
			t.Fatalf("%s: no times lists", r.ID)
		}
		for i := range h.Times {
			checkTimes(t, r.ID+" times", len(h.Times[i]), h.Times[i])
		}
		curve := -1
		for _, n := range h.Networks {
			if c := n.SuccessScoreCurve; len(c.Values) > 0 && curve < 0 {
				curve = c.TimesIndex
			}
			if c := n.SuccessScoreCurve; len(c.Values) > 0 && c.TimesIndex != curve {
				t.Errorf("%s %s: score curve on list %d, the first curve on %d", r.ID, n.Slug, c.TimesIndex, curve)
			}
			if n.SuccessScoreSpark.TimesIndex != n.SuccessSpark.TimesIndex {
				t.Errorf("%s %s: score spark times %d, success spark %d", r.ID, n.Slug, n.SuccessScoreSpark.TimesIndex, n.SuccessSpark.TimesIndex)
			}
		}
		m := h.Market
		checkTimes(t, r.ID+" market successScoreCurve", len(m.SuccessScoreCurve.Values), m.SuccessScoreCurve.Times)
		checkTimes(t, r.ID+" market successSpark", len(m.SuccessSpark.Values), m.SuccessSpark.Times)
		checkTimes(t, r.ID+" market latencySpark", len(m.LatencySpark.Values), m.LatencySpark.Times)
		checkTimes(t, r.ID+" market successScoreSpark", len(m.SuccessScoreSpark.Values), m.SuccessScoreSpark.Times)
	}
}

// Every /v1/home network series' timesIndex points to a times list of its
// length (SERIES-TIMES-INDEX-1).
func TestEveryHomeNetworkSeriesHasItsTimesIndex(t *testing.T) {
	t.Parallel()
	pg, _ := homePages(t)
	for _, r := range Ranges {
		h := homeOf(t, pg, r.ID)
		for _, n := range h.Networks {
			score := HomeSeries{Values: n.SuccessScoreSpark.Values, TimesIndex: n.SuccessScoreSpark.TimesIndex}
			series := map[string]HomeSeries{"successScoreCurve": n.SuccessScoreCurve, "successSpark": n.SuccessSpark, "latencySpark": n.LatencySpark, "successScoreSpark": score}
			for name, s := range series {
				if i := s.TimesIndex; i < 0 || i >= len(h.Times) || len(h.Times[i]) != len(s.Values) {
					t.Errorf("%s %s %s: timesIndex %d, %d values, %d times lists", r.ID, n.Slug, name, i, len(s.Values), len(h.Times))
				}
			}
		}
	}
}

// The JSON names: a top-level times, times beside an inline spark's values,
// timesIndex on every network series, and no labels anywhere
// (TIME-LABELS-REMOVE-1, #988).
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
			SuccessScoreCurve, SuccessScoreSpark struct {
				Times []float64 `json:"times"`
			}
		} `json:"market"`
	}
	if err := json.Unmarshal(raw, &body); err != nil {
		t.Fatal(err)
	}
	if bytes.Contains(raw, []byte(`"labels"`)) {
		t.Error("the home answer still has labels")
	}
	m := body.Market
	if len(body.Times) == 0 || len(m.SuccessScoreCurve.Times) == 0 || len(m.SuccessScoreSpark.Times) == 0 {
		t.Errorf("times %d lists, market score curve %d, score spark %d", len(body.Times), len(m.SuccessScoreCurve.Times), len(m.SuccessScoreSpark.Times))
	}
	if len(body.Networks) == 0 {
		t.Fatal("no networks")
	}
	// successSpark is internal (SUCCESS-RATE-REMOVE-1, #900), and so is the
	// rate curve (SUCCESS-CURVE-1, #1072).
	for _, name := range []string{"successSpark", "curve"} {
		if _, ok := body.Networks[0][name]; ok {
			t.Errorf("networks[0] still serves %s", name)
		}
	}
	for _, name := range []string{"successScoreCurve", "latencySpark", "successScoreSpark"} {
		var s struct {
			TimesIndex *int `json:"timesIndex"`
		}
		if err := json.Unmarshal(body.Networks[0][name], &s); err != nil || s.TimesIndex == nil {
			t.Errorf("networks[0].%s: no timesIndex in %s (%v)", name, body.Networks[0][name], err)
		}
	}
}
