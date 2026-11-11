package build

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/http/httptest"
	"slices"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/Zeus-Development/proxy-index/api/internal/snap"
)

// ovWindow is a window with the overlap shares and the measured pool.
func ovWindow(exclusive, shared, pool float64) snap.Window {
	w := snap.EmptyWindow()
	w.Overlap.ExclusiveShare, w.Overlap.SharedShare, w.Pool.UniqueIps = Num(exclusive), Num(shared), Num(pool)
	return w
}

// ovEntry is a residential network with product id and windows.
func ovEntry(slug string, id int, windows map[string]snap.Window) *entry {
	return &entry{P: &model.Provider{Slug: slug, Name: "Net " + slug},
		Snap: map[string]*snap.Product{"residential": {ProductID: Num(id), Cohort: "network", Windows: windows}}}
}

func ovRow(window string, a, b int, shared, ipsA, ipsB float64) snap.OverlapRow {
	return snap.OverlapRow{Cohort: "network", Window: window, ProductA: Num(a), ProductB: Num(b),
		SharedIps: Num(shared), IpsA: Num(ipsA), IpsB: Num(ipsB)}
}

// ovContext: "solo" shares nothing (fully exclusive, no peers); "resold"
// has 60% of its pool inside "big", ten times its size, in 30d; "mixed"
// shares with big; "unknown" has no overlap figures. In 24h only big and
// mixed overlap, on other pools.
func ovContext() *Context {
	all := func(w snap.Window) map[string]snap.Window {
		return map[string]snap.Window{"24h": w, "7d": w, "30d": w}
	}
	big := all(ovWindow(0.93, 0.07, 9000))
	big["24h"] = ovWindow(0.98, 0.02, 4000)
	resold := all(ovWindow(0.35, 0.65, 900))
	resold["24h"] = ovWindow(1, 0, 300)
	mixed := all(ovWindow(0.5, 0.5, 2000))
	mixed["24h"] = ovWindow(0.9, 0.1, 800)
	return &Context{GeneratedAt: "now", Entries: []*entry{
		ovEntry("solo", 1, all(ovWindow(1, 0, 5000))), ovEntry("resold", 2, resold), ovEntry("big", 3, big),
		ovEntry("mixed", 4, mixed), ovEntry("unknown", 5, all(ovWindow(nan.F(), nan.F(), 700))),
		{P: &model.Provider{Slug: "catalog-only", Name: "Catalog only"}, Snap: map[string]*snap.Product{}},
	}, Snapshot: &snap.Snapshot{Overlap: []snap.OverlapRow{
		ovRow("30d", 2, 3, 600, 1000, 10000), ovRow("30d", 4, 3, 400, 2000, 10000),
		ovRow("30d", 3, 2, 600, 10000, 1000), // the same pair again, from the other side
		ovRow("24h", 3, 4, 80, 4000, 800),
		{Cohort: "other", Window: "30d", ProductA: 1, ProductB: 3, SharedIps: 50, IpsA: 5000, IpsB: 10000},
	}}}
}

func byNetwork(a *OverlapAnswer) map[string]OverlapNetwork {
	out := map[string]OverlapNetwork{}
	for _, n := range a.Networks {
		out[n.Slug] = n
	}
	return out
}

func TestAFullyExclusiveNetworkWithNoPeers(t *testing.T) {
	t.Parallel()
	a := OverlapFor(ovContext(), "residential", "30d")
	if len(a.Networks) != 5 || a.Networks[0].Slug != "solo" {
		t.Fatalf("networks %+v", a.Networks)
	}
	solo := byNetwork(a)["solo"]
	if model.SV(solo.Label) != LabelFullyExclusive || solo.LikelyUpstream != nil || len(solo.Peers) != 0 ||
		solo.PoolIps != 5000 || model.SV(solo.PoolBasis) != PoolBasisWindow || solo.ExclusiveIps != 5000 {
		t.Errorf("solo %+v", solo)
	}
	unknown := byNetwork(a)["unknown"]
	if unknown.Label != nil || unknown.ExclusiveIps.OK() || unknown.PoolIps != 700 {
		t.Errorf("unknown %+v", unknown)
	}
}

func TestAResellerPair(t *testing.T) {
	t.Parallel()
	a := OverlapFor(ovContext(), "residential", "30d")
	n := byNetwork(a)
	resold, big := n["resold"], n["big"]
	if resold.PoolIps != 1000 || model.SV(resold.PoolBasis) != PoolBasisOverlap || resold.ExclusiveIps != 350 ||
		model.SV(resold.Label) != LabelResale {
		t.Errorf("resold %+v", resold)
	}
	if u := resold.LikelyUpstream; u == nil || *u != (OverlapUpstream{Slug: "big", Name: "Net big", ShareOfOurs: 0.6}) {
		t.Errorf("resold upstream %+v", u)
	}
	if len(resold.Peers) != 1 || resold.Peers[0].ShareOfTheirs != 0.06 || resold.Peers[0].SharedIps != 600 {
		t.Errorf("resold peers %+v", resold.Peers)
	}
	// big: most shared first, and no upstream of its own.
	if big.LikelyUpstream != nil || len(big.Peers) != 2 || big.Peers[0].Slug != "resold" || big.Peers[1].Slug != "mixed" ||
		big.Peers[1].ShareOfOurs != 0.04 || big.Peers[1].ShareOfTheirs != 0.2 || model.SV(big.Label) != LabelMostlyExclusive {
		t.Errorf("big %+v", big)
	}
	if model.SV(n["mixed"].Label) != LabelMixed || n["mixed"].LikelyUpstream != nil {
		t.Errorf("mixed %+v", n["mixed"])
	}
	// No kind fleet entry: the distinct count is unknown, never pools minus pairs.
	m := a.Market
	sum := 5000.0 + 1000 + 10000 + 2000 + 700
	want := OverlapMarket{Networks: 5, SumOfPools: Num(sum), SharedIps: 1000, DistinctIps: nan,
		LikelyResellers: []string{"resold"}, DuplicatedShare: nan}
	if fmt.Sprint(m) != fmt.Sprint(want) {
		t.Errorf("market %+v, want %+v", m, want)
	}
}

func TestEachRangeReadsItsOwnPoolsAndPairs(t *testing.T) {
	t.Parallel()
	c := ovContext()
	day, month := OverlapFor(c, "residential", "24h"), OverlapFor(c, "residential", "30d")
	resold := byNetwork(day)["resold"]
	if model.SV(resold.Label) != LabelFullyExclusive || resold.LikelyUpstream != nil || resold.PoolIps != 300 {
		t.Errorf("24h resold %+v", resold)
	}
	big := byNetwork(day)["big"]
	if big.PoolIps != 4000 || len(big.Peers) != 1 || big.Peers[0].Slug != "mixed" || big.Peers[0].ShareOfTheirs != 0.1 {
		t.Errorf("24h big %+v", big)
	}
	if day.Market.SumOfPools != 5000+300+4000+800+700 || day.Market.SharedIps != 80 || len(day.Market.LikelyResellers) != 0 {
		t.Errorf("24h market %+v", day.Market)
	}
	if month.Market.SharedIps != 1000 {
		t.Errorf("30d market %+v", month.Market)
	}
}

func TestTheKindFleetEntryIsTheDistinctCount(t *testing.T) {
	t.Parallel()
	c := ovContext()
	c.Snapshot.Fleet = []snap.FleetRow{{Cohort: "*", Window: "7d", UniqueIps: 99}, {Cohort: "*:residential", Window: "7d", UniqueIps: 15000}}
	// 7d has no pair: every pool is its window's.
	m, sum := OverlapFor(c, "residential", "7d").Market, 17600.0
	if m.SumOfPools != Num(sum) || m.DistinctIps != 15000 || model.SV(m.Basis) != BasisKindFleet || m.DuplicatedShare != Num(1-15000/sum) {
		t.Errorf("7d market %+v", m)
	}
	// 30d has pairs but no kind entry: null with its reason.
	a := OverlapFor(c, "residential", "30d")
	if err := a.listNA(); err != nil {
		t.Fatal(err)
	}
	if m := a.Market; m.DistinctIps.OK() || m.DuplicatedShare.OK() || m.Basis != nil || !m.SharedIps.OK() {
		t.Errorf("30d market %+v", m)
	}
	for _, p := range []string{"market.basis", "market.distinctIps", "market.duplicatedShare"} {
		if r := a.ReasonOf(p); r != NAInsufficient {
			t.Errorf("30d %s: reason %q, want %s", p, r, NAInsufficient)
		}
	}
}

// Three networks share one IP, and each pair shares some more: the market's
// distinctIps is the kind fleet entry's union and duplicatedShare the true
// share, where the sum of pools minus every pair's shared IPs (the deleted
// formula) counts the IP held by all three once too few.
func TestAnIPOfThreeNetworksIsCountedOnce(t *testing.T) {
	t.Parallel()
	pools := map[int][]int{1: {0, 1, 2, 10, 11}, 2: {0, 1, 3, 20}, 3: {0, 2, 3, 30, 31, 32}} // IP 0 in all three
	union, sum := map[int]bool{}, 0.0
	all := func(w snap.Window) map[string]snap.Window { return map[string]snap.Window{"7d": w} }
	c := &Context{GeneratedAt: "now", Snapshot: &snap.Snapshot{}}
	for id := 1; id <= 3; id++ {
		for _, ip := range pools[id] {
			union[ip] = true
		}
		sum += float64(len(pools[id]))
		c.Entries = append(c.Entries, ovEntry(fmt.Sprintf("n%d", id), id, all(ovWindow(0.5, 0.5, float64(len(pools[id]))))))
		for other := id + 1; other <= 3; other++ {
			shared := 0
			for _, ip := range pools[id] {
				if slices.Contains(pools[other], ip) {
					shared++
				}
			}
			c.Snapshot.Overlap = append(c.Snapshot.Overlap, ovRow("7d", id, other, float64(shared), float64(len(pools[id])), float64(len(pools[other]))))
		}
	}
	c.Snapshot.Fleet = []snap.FleetRow{{Cohort: "*:residential", Window: "7d", UniqueIps: Num(len(union))}}
	m := OverlapFor(c, "residential", "7d").Market
	if len(union) != 10 || m.DistinctIps != 10 || m.SumOfPools != 15 || m.SharedIps != 6 || m.DuplicatedShare != Num(1-10/sum) ||
		model.SV(m.Basis) != BasisKindFleet {
		t.Errorf("market %+v, want 10 distinct of 15", m)
	}
	if pairwise := m.SumOfPools - m.SharedIps; pairwise == m.DistinctIps {
		t.Errorf("the pairwise formula gives the union %v: the fixture shows nothing", pairwise)
	}
}

func TestOwnershipLabelThresholds(t *testing.T) {
	t.Parallel()
	for _, c := range []struct {
		exclusive, shared float64
		want              string
	}{{1, 0, LabelFullyExclusive}, {0.9999, 0.0001, LabelMostlyExclusive}, {0.75, 0.25, LabelMostlyExclusive},
		{0.7499, 0.2501, LabelMixed}, {0.4, 0.6, LabelMixed}, {0.3999, 0.6001, LabelResale}, {0, 1, LabelResale}} {
		if got := model.SV(OwnershipLabel(Num(c.exclusive), Num(c.shared))); got != c.want {
			t.Errorf("%v/%v: %s, want %s", c.exclusive, c.shared, got, c.want)
		}
	}
	if OwnershipLabel(1, nan) != nil || OwnershipLabel(nan, 0) != nil {
		t.Error("an unknown share has a label")
	}
}

func TestLikelyUpstreamThresholds(t *testing.T) {
	t.Parallel()
	for _, c := range []struct {
		ours, theirs float64
		want         bool
	}{{0.45, 0.01, false}, {0.4501, 0.01, true}, {0.6, 0.2, false}, {0.6, 0.1999, true}, {0.9, nan.F(), false}} {
		got := LikelyUpstream([]OverlapPeer{{Slug: "up", ShareOfOurs: Num(c.ours), ShareOfTheirs: Num(c.theirs), name: "Up"}})
		if (got != nil) != c.want {
			t.Errorf("%v/%v: %+v", c.ours, c.theirs, got)
		}
	}
	if LikelyUpstream(nil) != nil {
		t.Error("no peers, an upstream")
	}
}

func TestOverlapBodiesPerKindAndRange(t *testing.T) {
	t.Parallel()
	c := ovContext()
	c.Entries = append(c.Entries, &entry{P: &model.Provider{Slug: "cell"},
		Snap: map[string]*snap.Product{"mobile": {ProductID: 9, Windows: map[string]snap.Window{}}}})
	bodies, err := buildOverlap(c)
	if err != nil || len(bodies) != 6 {
		t.Fatalf("%d bodies, %v", len(bodies), err)
	}
	if a := bodies["/v1/overlap/mobile/7d"].(*OverlapAnswer); len(a.Networks) != 1 || a.Networks[0].Label != nil ||
		a.Market.DuplicatedShare.OK() || a.Kind != "mobile" || a.Range != "7d" {
		t.Errorf("mobile %+v", a)
	}
	// Without a snapshot the default request still has its (empty) bodies.
	bodies, _ = buildOverlap(&Context{GeneratedAt: "now"})
	if a, ok := bodies["/v1/overlap/residential/30d"].(*OverlapAnswer); !ok || len(bodies) != 3 || len(a.Networks) != 0 ||
		a.Market.Networks != 0 {
		t.Errorf("no snapshot %v", bodies)
	}
}

func TestOverlapKey(t *testing.T) {
	t.Parallel()
	for url, want := range map[string]string{"/v1/overlap": "/v1/overlap/residential/30d",
		"/v1/overlap?range=24h": "/v1/overlap/residential/24h", "/v1/overlap?kind=mobile&range=7d": "/v1/overlap/mobile/7d"} {
		if got, err := overlapKey(httptest.NewRequest("GET", url, nil)); got != want || err != nil {
			t.Errorf("%s: %s %v", url, got, err)
		}
	}
	for _, url := range []string{"/v1/overlap?range=1y", "/v1/overlap?kind=Mobile", "/v1/overlap?slug=x"} {
		var bad BadRequest
		if _, err := overlapKey(httptest.NewRequest("GET", url, nil)); !errors.As(err, &bad) {
			t.Errorf("%s: %v", url, err)
		}
	}
}

// Every pair of 18 networks (the issue's scale) fits the answer cap.
func TestEveryPairOfEighteenNetworksFits(t *testing.T) {
	t.Parallel()
	c := &Context{Snapshot: &snap.Snapshot{}}
	for i := 1; i <= 18; i++ {
		w := ovWindow(0.123456789, 0.876543211, 1234567)
		c.Entries = append(c.Entries, ovEntry(fmt.Sprintf("network-%02d", i), i, map[string]snap.Window{"30d": w}))
		for j := 1; j < i; j++ {
			c.Snapshot.Overlap = append(c.Snapshot.Overlap, ovRow("30d", i, j, 123457, 1234567, 7654321))
		}
	}
	b, err := json.Marshal(OverlapFor(c, "residential", "30d"))
	if err != nil || len(b) > MaxAnswerBytes {
		t.Fatalf("%d bytes, %v", len(b), err)
	}
	t.Logf("18 networks, every pair: %d bytes", len(b))
}
