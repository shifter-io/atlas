package build

import (
	"encoding/json"
	"math"
	"strings"
	"testing"

	"github.com/shifter-io/atlas/api/internal/jsnum"
	"github.com/shifter-io/atlas/api/internal/model"
	"github.com/shifter-io/atlas/api/internal/snap"
	"github.com/shifter-io/atlas/api/internal/testscale"
)

// poolWindow is a run window whose pool has size IPs, known of them with a
// usage type and host of those a hosting one.
func poolWindow(host, known, size float64) snap.Window {
	w := snap.EmptyWindow()
	w.Metrics.Completed = 100
	w.Pool.DatacenterAsnShare, w.Pool.ClassificationCoverage, w.Pool.UniqueIps = Num(host/known), Num(known/size), Num(size)
	return w
}

// datacenterIps is the share's numerator: for a pool of size IPs, known of
// them with a usage type and host of those a hosting one, it is host, and
// datacenterIps ÷ the IPs with a usage type is datacenterAsnShare. In a
// window with hour-formula figures both are served × breakdown_scale (K8).
func TestDatacenterIpsIsTheSharesNumerator(t *testing.T) {
	t.Parallel()
	pools := [][3]float64{{0, 1, 1}, {1, 3, 7}, {20, 4957, 4967}, {39, 4967, 4967}, {12345, 154000, 154770}}
	for size := 1.0; size <= 60; size++ {
		for known := 1.0; known <= size; known++ {
			for host := 0.0; host <= known; host++ {
				pools = append(pools, [3]float64{host, known, size})
			}
		}
	}
	for _, c := range pools {
		host, known, size := c[0], c[1], c[2]
		w := poolWindow(host, known, size)
		got := datacenterIps(w)
		classified := math.Round(w.Pool.ClassificationCoverage.F() * w.Pool.UniqueIps.F())
		if got != Num(host) || math.Abs(got.F()/classified-w.Pool.DatacenterAsnShare.F()) > 1e-12 {
			t.Fatalf("host %v of %v known of %v: datacenterIps %v (÷ %v = %v, share %v)", host, known, size, got,
				classified, got.F()/classified, w.Pool.DatacenterAsnShare)
		}
	}
	w := poolWindow(12345, 154000, 154770)
	w.Estimated = &snap.Estimated{UniqueIps: snap.EstimatedValue{Value: 400000}, BreakdownScale: 2.5}
	if got := datacenterIps(w); got != 30863 || math.Abs(got.F()/scaledIps(w, 154000).F()-w.Pool.DatacenterAsnShare.F()) > 1e-4 {
		t.Errorf("estimated window: datacenterIps %v, want 12345 × 2.5 rounded half up", got)
	}
	for name, edit := range map[string]func(*snap.Window){
		"no share":    func(w *snap.Window) { w.Pool.DatacenterAsnShare = nan },
		"no coverage": func(w *snap.Window) { w.Pool.ClassificationCoverage = nan },
		"no pool":     func(w *snap.Window) { w.Pool.UniqueIps = nan },
	} {
		w := poolWindow(1, 10, 10)
		edit(&w)
		if got := datacenterIps(w); got.OK() {
			t.Errorf("%s: datacenterIps %v, want null", name, got)
		}
	}
}

// Every network's stats, ranges, rankings row and home row carry the
// window pool's datacenterIps next to its datacenterAsnShare.
func TestDatacenterIpsIsServedNextToTheShare(t *testing.T) {
	t.Parallel()
	in, err := testscale.Inputs(testscale.Shape{Networks: 3, Countries: 1, Exits: 3, Tail: 4})
	if err != nil {
		t.Fatal(err)
	}
	res, err := Build(in)
	if err != nil {
		t.Fatal(err)
	}
	set := 0
	for _, r := range Ranges {
		home := res.Pages.Registered[homePath(r.ID)].(*HomeAnswer)
		homeIps := map[string]Num{}
		for _, n := range home.Networks {
			homeIps[n.Slug] = n.DatacenterIps
		}
		for _, row := range res.Pages.Rankings[r.ID].Networks {
			ans := res.Pages.Network[row.Slug+"/"+row.Kind]
			stats, totals := ans.Stats[r.ID], ans.Ranges[r.ID]
			if !sameNum(stats.DatacenterIps, row.DatacenterIps) || !sameNum(totals.DatacenterIps, row.DatacenterIps) ||
				row.DatacenterIps.OK() != row.DatacenterAsnShare.OK() {
				t.Errorf("%s %s: stats %v, ranges %v, row %v (share %v)", r.ID, row.Slug, stats.DatacenterIps, totals.DatacenterIps,
					row.DatacenterIps, row.DatacenterAsnShare)
			}
			if ips, ok := homeIps[row.Slug]; ok && !sameNum(ips, row.DatacenterIps) {
				t.Errorf("%s %s: home %v, row %v", r.ID, row.Slug, ips, row.DatacenterIps)
			}
			if row.DatacenterIps.OK() {
				set++
			}
		}
	}
	if set == 0 {
		t.Error("no row has a datacenterIps")
	}
}

// Every point of a network chart's success history has its score among the
// networks of the kind, as the rankings' successScoreSpark thins it, to 2
// decimals; the all-networks chart's fleet points have none.
func TestChartPointsCarryTheirScore(t *testing.T) {
	t.Parallel()
	in, err := testscale.Inputs(testscale.Shape{Networks: 4, Countries: 1, Exits: 3, Tail: 4, SuccessScores: true})
	if err != nil {
		t.Fatal(err)
	}
	res, err := Build(in)
	if err != nil {
		t.Fatal(err)
	}
	compared := 0
	for key, rows := range res.Pages.RankingsByKind {
		rangeID, kind, _ := strings.Cut(key, "/")
		for _, row := range rows.Networks {
			c := res.Pages.Charts[row.Slug+"/"+kind+"/"+rangeID]
			if c == nil {
				continue
			}
			scores := make([]model.MetricScore, len(c.Success))
			for i, p := range c.Success {
				if p.PointScore == nil || p.Status == nil || p.Score.OK() != (model.SV(p.Status) == scoreRanked) ||
					p.Score.OK() && p.Score != Num(jsnum.FixedNum(p.Score.F(), 2)) {
					t.Fatalf("%s %s point %d: %+v", key, row.Slug, i, p.PointScore)
				}
				scores[i] = model.MetricScore{Score: p.Score, Status: model.SV(p.Status)}
				if st := model.SV(p.Status); st == scoreRanked {
					compared++ // the point was compared among 3 or more networks
				}
			}
			thinned, spark := thinScores(scores, TrendPoints), row.SuccessScoreSpark
			for i := range spark.Values {
				if model.SV(thinned.Statuses[i]) != model.SV(spark.Statuses[i]) || !sameNum(thinned.Values[i], Num(jsnum.FixedNum(spark.Values[i].F(), 2))) {
					t.Errorf("%s %s spark point %d: chart %v %v, rankings %v %v", key, row.Slug, i, thinned.Values[i],
						model.SV(thinned.Statuses[i]), spark.Values[i], model.SV(spark.Statuses[i]))
				}
			}
		}
	}
	if compared == 0 {
		t.Error("no chart point compared among the networks")
	}
	all := res.Pages.Charts[AllNetworks+"/"+AllNetworks+"/24h"]
	raw, err := json.Marshal(all.Success)
	if err != nil {
		t.Fatal(err)
	}
	if len(all.Success) == 0 || strings.Contains(string(raw), "score") {
		t.Errorf("all-networks chart success %s", raw)
	}
}

// A chart point's score is its point score to 2 decimals, with its status;
// a chart whose scores do not match its points scores none of them.
func TestChartPointsWithoutMatchingScoresAreNotScored(t *testing.T) {
	t.Parallel()
	points := chartPoints([]model.SeriesPoint{{T: 10, V: 0.9}, {T: 5, V: 0.8}})
	scorePoints(points, []model.MetricScore{{Score: 7.256, Status: scoreRanked}, {Score: nan, Status: scoreTooFewNetworks}})
	if raw, err := json.Marshal(points); err != nil ||
		string(raw) != `[{"t":10,"score":7.26,"status":"ranked"},{"t":5,"score":null,"status":"too_few_networks"}]` {
		t.Errorf("scored points %s (%v)", raw, err)
	}
	scorePoints(points, []model.MetricScore{{Score: 7, Status: scoreRanked}})
	for _, p := range points {
		if p.Score.OK() || model.SV(p.Status) != notEnoughData {
			t.Errorf("point %+v", p.PointScore)
		}
	}
	raw, err := json.Marshal(points[0])
	if err != nil {
		t.Fatal(err)
	}
	// The point's rate is internal (SUCCESS-RATE-REMOVE-1, #900).
	if string(raw) != `{"t":10,"score":null,"status":"not_enough_data"}` || points[0].V != 0.9 {
		t.Errorf("point JSON %s", raw)
	}
}
