package build

import (
	"encoding/json"
	"errors"
	"math"
	"net/http/httptest"
	"slices"
	"testing"

	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/snap"
	"github.com/Zeus-Development/atlas/api/internal/store"
	"github.com/Zeus-Development/atlas/api/internal/testscale"
)

// homePages is rowsPages's build (net00 and net01 priced, net02 unpriced,
// one unmeasured network) with 40 test events, net01's browser overall
// removed from every window.
func homePages(t *testing.T) (*Pages, *model.Dataset) {
	t.Helper()
	in, err := testscale.Inputs(testscale.Shape{Networks: 3, Countries: 1, Exits: 3, Tail: 5, Unmeasured: 1, Browser: true, Events: 40})
	if err != nil {
		t.Fatal(err)
	}
	in.Snapshot = editSnapshot(t, in.Snapshot, func(doc map[string]any) {
		for _, p := range doc["products"].([]any) {
			if p := p.(map[string]any); p["provider"] == "net01" {
				for _, w := range p["windows"].(map[string]any) {
					delete(w.(map[string]any), "browser_overall")
				}
			}
		}
		setPricing(doc, "net00", 4, 3, 3)
		setPricing(doc, "net01", 1, math.NaN(), 1)
	})
	f := func(v float64) *float64 { return &v }
	in.Pricing = append(in.Pricing, store.PricingRow{ProviderSlug: "net00", Category: "residential", PricePerGb: f(4), PricePerGbAtScale: f(3)},
		store.PricingRow{ProviderSlug: "net01", Category: "residential", PricePerGb: f(1)})
	res, err := Build(in)
	if err != nil {
		t.Fatal(err)
	}
	return res.Pages, res.Dataset
}

func homeOf(t *testing.T, pg *Pages, rangeID string) *HomeAnswer {
	t.Helper()
	h, ok := pg.Registered[homePath(rangeID)].(*HomeAnswer)
	if !ok {
		t.Fatalf("no %s body (unbuilt: %v)", homePath(rangeID), pg.Unbuilt)
	}
	return h
}

func homeNet(t *testing.T, h *HomeAnswer, slug string) HomeNetwork {
	t.Helper()
	for _, n := range h.Networks {
		if n.Slug == slug {
			return n
		}
	}
	t.Fatalf("%s is not on the %s home", slug, h.Range)
	return HomeNetwork{}
}

// Each range's networks are its ranking rows, in their order and with their
// figures; every series' labels are one entry of the shared list, as long
// as its values, each distinct times list once; the curve and sparks keep the old site's point counts.
func TestHomeNetworksAreTheRankingRowsWithTheirSeries(t *testing.T) {
	t.Parallel()
	pg, ds := homePages(t)
	for _, r := range Ranges {
		h, rows := homeOf(t, pg, r.ID), pg.Rankings[r.ID].Networks
		if len(h.Networks) != len(rows) || len(h.Networks) != 4 {
			t.Fatalf("%s: %d networks, %d rows", r.ID, len(h.Networks), len(rows))
		}
		for i, n := range h.Networks {
			row := rows[i]
			if n.Slug != row.Slug || !sameNum(n.Standing.Rank, row.Standing.Rank) || !sameNum(n.ListRate, row.ListRate) || !sameNum(n.Score, row.Score) ||
				!sameNum(n.SuccessRate, row.SuccessRate) || !sameNum(n.Countries, row.Countries) {
				t.Errorf("%s #%d: %+v, row %+v", r.ID, i, n, row)
			}
			for name, series := range map[string]HomeSeries{"curve": n.Curve, "success": n.SuccessSpark, "latency": n.LatencySpark} {
				if series.TimesIndex < 0 || series.TimesIndex >= len(h.Times) || len(h.Times[series.TimesIndex]) != len(series.Values) {
					t.Errorf("%s %s %s: times %d of %d", r.ID, n.Slug, name, series.TimesIndex, len(h.Times))
				}
			}
			if len(n.SuccessSpark.Values) > homeNetworkSpark || len(n.LatencySpark.Values) > homeNetworkSpark ||
				r.Days == 1 && len(n.Curve.Values) > homeCurve24h {
				t.Errorf("%s %s: %d, %d, %d points", r.ID, n.Slug, len(n.Curve.Values), len(n.SuccessSpark.Values), len(n.LatencySpark.Values))
			}
		}
		// One entry per distinct times list (SPARK-TIMES-1).
		for i := range h.Times {
			for j := i + 1; j < len(h.Times); j++ {
				if slices.Equal(h.Times[i], h.Times[j]) {
					t.Errorf("%s: times %d and %d are the same", r.ID, i, j)
				}
			}
		}
		if !slices.Equal(tapeIDs(h.Tape), tapeIDs(tabHeads(ds.Tape))) || len(h.Tape) == 0 {
			t.Errorf("%s: %d tape rows of %d", r.ID, len(h.Tape), len(ds.Tape))
		}
		if len(h.Profiles) != len(ds.Profiles) || h.GeneratedAt != ds.GeneratedAt || h.Range != r.ID {
			t.Errorf("%s: head %s %s, %d profiles", r.ID, h.GeneratedAt, h.Range, len(h.Profiles))
		}
	}
}

// A network without a browser overall has a null one, the others keep
// theirs; the unpriced network has no list rate or value rank; a measured
// network has its schedule, curve and claim share rule, the unmeasured one
// no series.
func TestHomeNetworkFigures(t *testing.T) {
	t.Parallel()
	pg, _ := homePages(t)
	h := homeOf(t, pg, "30d")
	if n := homeNet(t, h, "net01"); n.BrowserOverall != nil {
		t.Errorf("net01 browser overall %v", n.BrowserOverall)
	}
	n0 := homeNet(t, h, "net00")
	if n0.BrowserOverall == nil || n0.ListRate != 3 || n0.ValueRankAllTargets == nil {
		t.Errorf("net00 %+v", n0)
	}
	if len(n0.Curve.Values) == 0 || !n0.Requests.OK() {
		t.Errorf("net00 %d curve points", len(n0.Curve.Values))
	}
	if n0.Claimed.OK() || n0.ClaimShare.OK() {
		t.Errorf("net00 has no claimed pool: %v, %v", n0.Claimed, n0.ClaimShare)
	}
	if n := homeNet(t, h, "net02"); n.ListRate.OK() || n.ValueRankAllTargets != nil {
		t.Errorf("net02 is unpriced: %v, %v", n.ListRate, n.ValueRankAllTargets)
	}
	for _, n := range h.Networks {
		if !slices.Contains([]string{"net00", "net01", "net02"}, n.Slug) && (len(n.Curve.Values) != 0 || n.Requests.OK()) {
			t.Errorf("unmeasured %s: %+v", n.Slug, n)
		}
	}
}

// The market: requests, distinct IPs and countries are the all-networks
// answer's; the cheapest and dearest are the priced networks; the spread
// leaves the unpriced one out; the measured target count and the networks
// with a published group.
func TestHomeMarket(t *testing.T) {
	t.Parallel()
	pg, ds := homePages(t)
	for _, r := range Ranges {
		m, all := homeOf(t, pg, r.ID).Market, pg.Network[AllNetworks+"/"+AllNetworks].Ranges[r.ID]
		if m.Requests != all.Requests || !sameNum(m.UniqueIps, all.UniqueIps) || m.Countries != Num(all.ExitCountries) {
			t.Errorf("%s: market %v %v %v, all networks %+v", r.ID, m.Requests, m.UniqueIps, m.Countries, all)
		}
		if m.Cheapest == nil || m.Cheapest.Slug != "net01" || m.Cheapest.Value != 1 || m.Dearest == nil || m.Dearest.Slug != "net00" ||
			m.Dearest.Value != 3 {
			t.Errorf("%s: cheapest %+v, dearest %+v", r.ID, m.Cheapest, m.Dearest)
		}
		if m.Networks != 3 || !m.BandwidthGb.OK() || m.Targets != targetCounts(ds.Targets).Measured || m.Fastest == nil || m.MostReliable == nil {
			t.Errorf("%s: %d networks, %d targets, %+v %+v", r.ID, m.Networks, m.Targets, m.Fastest, m.MostReliable)
		}
		if len(m.LatencySpark.Values) > 24 || len(m.SuccessSpark.Values) > 24 || len(m.Curve.Times) != len(m.Curve.Values) {
			t.Errorf("%s: sparks %d %d, curve %d", r.ID, len(m.LatencySpark.Values), len(m.SuccessSpark.Values), len(m.Curve.Values))
		}
		body, err := json.Marshal(homeOf(t, pg, r.ID))
		if err != nil || len(body) > MaxAnswerBytes {
			t.Errorf("%s: %d bytes, %v", r.ID, len(body), err)
		}
	}
}

func TestHomeFleetObservedDays(t *testing.T) {
	t.Parallel()
	pts := func(minutes float64) []model.SeriesPoint { return []model.SeriesPoint{{T: Num(minutes)}, {T: 0}} }
	for _, c := range []struct {
		history []model.SeriesPoint
		rangeID string
		want    Num
	}{{nil, "30d", nan}, {pts(30), "24h", 0}, {pts(1440), "24h", nan}, {pts(2.5 * 1440), "7d", 3}, {pts(30 * 1440), "30d", nan},
		{[]model.SeriesPoint{{T: nan}}, "7d", nan}} {
		if got := fleetObservedDays(c.history, c.rangeID); !sameNum(got, c.want) {
			t.Errorf("%v %s: %v, want %v", c.history, c.rangeID, got, c.want)
		}
	}
}

// The gauge shares weigh each non-datacenter window by its exit IPs; the
// incidents add every network's.
func TestHomePoolShares(t *testing.T) {
	t.Parallel()
	net := func(slug, kind string, ips, geo, dc float64) *entry {
		w := snap.EmptyWindow()
		w.Pool.UniqueIps, w.Pool.GeoAccuracy, w.Pool.DatacenterAsnShare = Num(ips), Num(geo), Num(dc)
		return &entry{P: &model.Provider{Slug: slug, PrimaryKind: kind},
			Snap: map[string]*snap.Product{kind: {Windows: map[string]snap.Window{"7d": w}}}}
	}
	count := func(n float64) map[string]*WindowStats { return map[string]*WindowStats{"7d": {IncidentCount: Num(n)}} }
	fleet := []*entry{net("a", "residential", 100, 0.9, 0.1), net("b", "mobile", 300, 0.5, math.NaN()),
		net("dc", "datacenter", 1000, 0, 1), {P: &model.Provider{Slug: "none", PrimaryKind: "residential"}}}
	stats := map[string]map[string]*WindowStats{"a": count(1), "b": count(5), "dc": count(math.NaN()), "none": count(0)}
	authentic, misSold, incidents := poolShares(fleet, stats, "7d")
	if math.Abs(authentic.F()-0.6) > 1e-9 || math.Abs(misSold.F()-0.1) > 1e-9 || incidents != 6 {
		t.Errorf("authentic %v, mis-sold %v, incidents %v", authentic, misSold, incidents)
	}
	if a, m, i := poolShares(nil, nil, "7d"); a.OK() || m.OK() || i.OK() {
		t.Errorf("no networks: %v %v %v", a, m, i)
	}
}

func TestHomeKeyAndErrors(t *testing.T) {
	t.Parallel()
	a := answers["home"]
	for url, want := range map[string]string{"/v1/home": "/v1/home/30d", "/v1/home?range=24h": "/v1/home/24h"} {
		if got, err := a.Key(httptest.NewRequest("GET", url, nil)); got != want || err != nil {
			t.Errorf("%s: %s, %v", url, got, err)
		}
	}
	for _, url := range []string{"/v1/home?range=1y", "/v1/home?kind=residential"} {
		var bad APIError
		if _, err := a.Key(httptest.NewRequest("GET", url, nil)); !errors.As(err, &bad) {
			t.Errorf("%s: %v", url, err)
		}
	}
	if _, err := buildHome(&Context{Dataset: &model.Dataset{}}); err == nil {
		t.Error("a build without pages answers")
	}
	if _, err := buildHome(&Context{Dataset: &model.Dataset{}, Pages: &Pages{}}); err == nil {
		t.Error("a build without rankings answers")
	}
}
