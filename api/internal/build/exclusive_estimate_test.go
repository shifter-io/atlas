package build

import (
	"fmt"
	"math"
	"math/rand"
	"slices"
	"testing"

	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/snap"
)

// estWindow is ovWindow with a run and the hour formula's unique IPs (NaN:
// none), the window's served uniqueIps (EXCLUSIVE-ESTIMATE-1).
func estWindow(exclusive, shared, pool, estimate float64) snap.Window {
	w := ovWindow(exclusive, shared, pool)
	w.Metrics.Completed = 10
	w.Estimated = &snap.Estimated{UniqueIps: snap.EstimatedValue{Value: Num(estimate)}, BreakdownScale: nan}
	return w
}

// estContext is ovContext with every network run and estimated at about
// 2.2 × its observed pool (24h 2.0, 7d 2.4), but solo's 7d has no estimate.
func estContext() *Context {
	c := ovContext()
	factor := map[string]float64{"24h": 2.0, "7d": 2.4, "30d": 2.2}
	for _, e := range c.Entries {
		for _, sp := range e.Snap {
			for r, w := range sp.Windows {
				sp.Windows[r] = estWindow(w.Overlap.ExclusiveShare.F(), w.Overlap.SharedShare.F(), w.Pool.UniqueIps.F(),
					math.Round(w.Pool.UniqueIps.F()*factor[r]))
			}
		}
	}
	solo := c.Entries[0].Snap["residential"]
	solo.Windows["7d"] = estWindow(1, 0, 5000, nan.F())
	return c
}

// Every range: poolIps is the window's uniqueIps (ipsOf), exclusiveIps that
// × the measured share, and the observed figures are the measured pool's.
func TestExclusiveIpsAreTheUniqueIpsTimesTheShare(t *testing.T) {
	t.Parallel()
	c := estContext()
	for _, r := range Ranges {
		a := OverlapFor(c, "residential", r.ID)
		for i, n := range a.Networks {
			w := c.Entries[i].Snap["residential"].Window(r.ID)
			if n.PoolIps != ipsOf(w) || n.ExclusiveIps.OK() != n.ExclusiveShare.OK() ||
				n.ExclusiveShare.OK() && n.ExclusiveIps != Num(math.Round(ipsOf(w).F()*n.ExclusiveShare.F())) {
				t.Errorf("%s %s: pool %v exclusive %v, uniqueIps %v share %v", r.ID, n.Slug, n.PoolIps, n.ExclusiveIps, ipsOf(w), n.ExclusiveShare)
			}
			if n.ExclusiveShare.OK() && n.ObservedExclusiveIps != Num(math.Round(n.ObservedPoolIps.F()*n.ExclusiveShare.F())) {
				t.Errorf("%s %s: observed %v of %v", r.ID, n.Slug, n.ObservedExclusiveIps, n.ObservedPoolIps)
			}
		}
	}
	big := byNetwork(OverlapFor(c, "residential", "30d"))["big"]
	if big.PoolIps != 19800 || model.SV(big.PoolBasis) != PoolBasisUnique || big.ObservedPoolIps != 10000 ||
		big.ExclusiveIps != 18414 || big.ObservedExclusiveIps != 9300 {
		t.Errorf("big 30d %+v", big)
	}
}

// A network with no peers is wholly exclusive: exclusiveIps is uniqueIps.
// Without an estimate the pool falls back to the observed one and its basis.
func TestNoPeersAndNoEstimate(t *testing.T) {
	t.Parallel()
	c := estContext()
	solo := byNetwork(OverlapFor(c, "residential", "30d"))["solo"]
	if solo.ExclusiveIps != 11000 || solo.PoolIps != 11000 || solo.ObservedPoolIps != 5000 || len(solo.Peers) != 0 {
		t.Errorf("solo 30d %+v", solo)
	}
	week := byNetwork(OverlapFor(c, "residential", "7d"))["solo"]
	if week.PoolIps != 5000 || model.SV(week.PoolBasis) != PoolBasisWindow || week.ExclusiveIps != 5000 || week.ObservedExclusiveIps != 5000 {
		t.Errorf("solo 7d without an estimate %+v", week)
	}
}

// exclusiveIps + the shared IPs (sharedShare × poolIps) is poolIps, and each
// pair's sharedIps is shareOfOurs × our poolIps, all within rounding.
func TestExclusiveAndSharedAddUpToThePool(t *testing.T) {
	t.Parallel()
	for _, r := range Ranges {
		for _, n := range OverlapFor(estContext(), "residential", r.ID).Networks {
			if !n.ExclusiveIps.OK() {
				continue
			}
			if d := n.ExclusiveIps.F() + math.Round(n.SharedShare.F()*n.PoolIps.F()) - n.PoolIps.F(); math.Abs(d) > 1 {
				t.Errorf("%s %s: %v + shared ≠ %v", r.ID, n.Slug, n.ExclusiveIps, n.PoolIps)
			}
			for _, p := range n.Peers {
				if p.SharedIps != Num(math.Round(p.ShareOfOurs.F()*n.PoolIps.F())) {
					t.Errorf("%s %s with %s: shared %v", r.ID, n.Slug, p.Slug, p.SharedIps)
				}
			}
		}
	}
	resold := byNetwork(OverlapFor(estContext(), "residential", "30d"))["resold"]
	// resold: its window's 900 IPs × 2.2; 60% of it inside big.
	if len(resold.Peers) != 1 || resold.Peers[0].SharedIps != 1188 || resold.PoolIps != 1980 || resold.ExclusiveIps != 693 {
		t.Errorf("resold %+v", resold)
	}
}

// shareView is every share, label, upstream and pair order of an answer.
func shareView(a *OverlapAnswer) string {
	out := fmt.Sprint(a.Market.DuplicatedShare, a.Market.LikelyResellers)
	for _, n := range a.Networks {
		out += fmt.Sprint(n.Slug, n.ExclusiveShare, n.SharedShare, model.SV(n.Label), n.LikelyUpstream)
		for _, p := range n.Peers {
			out += fmt.Sprint(p.Slug, p.ShareOfOurs, p.ShareOfTheirs)
		}
	}
	return out
}

// randomOverlap is n networks with random pools, estimates, pairs and a
// kind fleet entry; estimated false strips every estimate.
func randomOverlap(rng *rand.Rand, n int, estimated bool) *Context {
	c := &Context{GeneratedAt: "now", Snapshot: &snap.Snapshot{}}
	pools := make([]float64, n+1)
	for id := 1; id <= n; id++ {
		pools[id] = float64(100 + rng.Intn(100000))
		ex, none := rng.Float64(), rng.Intn(5) == 0
		est := math.Round(pools[id] * (1 + 2*rng.Float64()))
		if !estimated || none {
			est = nan.F()
		}
		c.Entries = append(c.Entries, ovEntry(fmt.Sprint("n", id), id, map[string]snap.Window{"30d": estWindow(ex, 1-ex, pools[id], est)}))
	}
	for a := 1; a <= n; a++ {
		for b := a + 1; b <= n; b++ {
			if rng.Intn(2) == 0 {
				shared := float64(rng.Intn(int(min(pools[a], pools[b]))))
				c.Snapshot.Overlap = append(c.Snapshot.Overlap, ovRow("30d", a, b, shared, pools[a], pools[b]))
			}
		}
	}
	c.Snapshot.Fleet = []snap.FleetRow{{Cohort: "*:residential", Window: "30d", UniqueIps: Num(math.Floor(pools[1] * float64(n) / 2))}}
	return c
}

// Property: the estimate changes counts only. Every share, label, upstream,
// pair order and duplicatedShare is the one the observed pools give.
func TestTheEstimateLeavesEveryShareUnchanged(t *testing.T) {
	t.Parallel()
	for seed := int64(1); seed <= 200; seed++ {
		n := 2 + int(seed%6)
		with := OverlapFor(randomOverlap(rand.New(rand.NewSource(seed)), n, true), "residential", "30d")
		without := OverlapFor(randomOverlap(rand.New(rand.NewSource(seed)), n, false), "residential", "30d")
		if got, want := shareView(with), shareView(without); got != want {
			t.Fatalf("seed %d: shares\n%s\nwant\n%s", seed, got, want)
		}
		m, o := with.Market, without.Market
		if o.SumOfPools != m.ObservedSumOfPools || math.Abs(m.DistinctIps.F()/m.SumOfPools.F()-o.DistinctIps.F()/o.SumOfPools.F()) > 1/m.SumOfPools.F() {
			t.Errorf("seed %d: market %+v, observed %+v", seed, m, o)
		}
	}
}

// The market's counts are on the estimated pools, by one factor, so
// duplicatedShare is the measured one.
func TestTheMarketCountsFollowTheEstimate(t *testing.T) {
	t.Parallel()
	c := estContext()
	c.Snapshot.Fleet = []snap.FleetRow{{Cohort: "*:residential", Window: "30d", UniqueIps: 15000}}
	m := OverlapFor(c, "residential", "30d").Market
	// Observed: solo's and unknown's windows, the others' ips_ours; each
	// estimate is its window's pool × 2.2.
	observed, est := 5000.0+1000+10000+2000+700, 11000.0+1980+19800+4400+1540
	if m.ObservedSumOfPools != Num(observed) || m.SumOfPools != Num(est) || m.SharedIps != Num(math.Round(1000*est/observed)) ||
		m.DistinctIps != Num(math.Round(15000*est/observed)) || m.DuplicatedShare != Num(1-15000/observed) {
		t.Errorf("market %+v", m)
	}
}

// The score and its ties follow the estimated exclusiveIps: a smaller
// observed pool with a larger estimate leads, and equal estimates share.
func TestTheScoreRanksTheEstimatedExclusiveIps(t *testing.T) {
	t.Parallel()
	c := &Context{GeneratedAt: "now", Snapshot: &snap.Snapshot{}, Entries: []*entry{
		ovEntry("a", 1, map[string]snap.Window{"30d": estWindow(1, 0, 1000, 3000)}),
		ovEntry("b", 2, map[string]snap.Window{"30d": estWindow(1, 0, 2000, 3000)}),
		ovEntry("c", 3, map[string]snap.Window{"30d": estWindow(1, 0, 2500, 1500)}),
	}}
	got := slugsOf(OverlapFor(c, "residential", "30d").Networks, scoreOf)
	if want := []string{"10/1.5/3/ranked", "10/1.5/3/ranked", "5/3/3/ranked"}; !slices.Equal(got, want) {
		t.Errorf("scores %v, want %v", got, want)
	}
}
