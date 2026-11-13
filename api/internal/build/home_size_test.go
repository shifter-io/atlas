package build

import (
	"encoding/json"
	"math"
	"testing"

	"github.com/Zeus-Development/atlas/api/internal/testscale"
)

// homeBodyMax is the /v1/home size rule (HOME-SIZE-1, #905): every range at
// most 180 KB, room under MaxAnswerBytes at the real shape, so a cap problem
// shows here and not in production (build 17's 7d was 215,345 bytes before).
const homeBodyMax = 180 << 10

// TestEveryHomeBodyFitsWithRoom builds /v1/home for 18 networks at the page
// latency test's week shape (about 250 curve points per network in 7d and
// 30d, the success scores at full width) with each network's own series gaps
// (testscale.AddGaps: a label array per network, as in production) and
// fails when a range is over homeBodyMax.
func TestEveryHomeBodyFitsWithRoom(t *testing.T) {
	t.Parallel()
	in, err := testscale.WeekInputs(testscale.Shape{Networks: 18, Countries: 1, Exits: 3, Tail: 30, Browser: true, SuccessScores: true, Gaps: true})
	if err != nil {
		t.Fatal(err)
	}
	res, err := Build(in)
	if err != nil {
		t.Fatal(err)
	}
	for _, r := range Ranges {
		body := res.Pages.Registered[homePath(r.ID)].(*HomeAnswer)
		raw, err := json.Marshal(body)
		if err != nil {
			t.Fatal(err)
		}
		curve := len(body.Networks[0].Curve.Values)
		times := 0
		for _, l := range body.Times {
			times += len(l)
		}
		t.Logf("/v1/home/%s: %d bytes, %d networks, %d curve points, %d label arrays, %d times", r.ID, len(raw), len(body.Networks), curve, len(body.Labels), times)
		if len(body.Networks) != 18 || (r.Days > 1 && (curve < 150 || len(body.Labels) < 18)) {
			t.Errorf("%s: %d networks, %d curve points, %d label arrays: not the real shape", r.ID, len(body.Networks), curve, len(body.Labels))
		}
		if len(raw) > homeBodyMax {
			t.Errorf("/v1/home/%s is %d bytes, above %d", r.ID, len(raw), homeBodyMax)
		}
	}
}

// TestHomeSeriesAreRoundedAndFiguresAreNot checks the decimals of every home
// chart series (rates 4, latencies 0, score sparks 2), that null points stay
// null, and that the rows' successScore is compact and unrounded.
func TestHomeSeriesAreRoundedAndFiguresAreNot(t *testing.T) {
	t.Parallel()
	res := targetsScaleBuild(t, testscale.WeekInputs)
	home := res.Pages.Registered[homePath("7d")].(*HomeAnswer)
	check := func(name string, values []Num, dp int) {
		t.Helper()
		for _, v := range values {
			if v.OK() && math.Abs(v.F()*math.Pow10(dp)-math.Round(v.F()*math.Pow10(dp))) > 1e-6 {
				t.Errorf("%s: %v has more than %d decimals", name, v.F(), dp)
				return
			}
		}
	}
	m := home.Market
	check("market curve", m.Curve.Values, homeRateDecimals)
	check("market successSpark", m.SuccessSpark.Values, homeRateDecimals)
	check("market latencySpark", m.LatencySpark.Values, homeLatencyDecimals)
	full := false
	for _, n := range home.Networks {
		check(n.Slug+" curve", n.Curve.Values, homeRateDecimals)
		check(n.Slug+" successSpark", n.SuccessSpark.Values, homeRateDecimals)
		check(n.Slug+" latencySpark", n.LatencySpark.Values, homeLatencyDecimals)
		check(n.Slug+" successScoreSpark", n.SuccessScoreSpark.Values, homeScoreDecimals)
		if s := n.SuccessScore; s != nil && s.Score.OK() && s.Score.F() != math.Round(s.Score.F()*100)/100 {
			full = true
		}
		raw, err := json.Marshal(n.SuccessScore)
		if err != nil {
			t.Fatal(err)
		}
		var keys map[string]any
		if err := json.Unmarshal(raw, &keys); err != nil {
			t.Fatal(err)
		}
		if _, ok := keys["targets"]; ok || len(keys) != 4 {
			t.Errorf("%s successScore %s: want {score, rank, rankedOf, status}", n.Slug, raw)
		}
	}
	if !full {
		t.Error("no row's successScore.score keeps its full precision")
	}
	got := roundedValues([]Num{0.123456, nan, 7.5}, 2)
	if got[0] != 0.12 || got[1].OK() || got[2] != 7.5 {
		t.Errorf("roundedValues: %v", got)
	}
}
