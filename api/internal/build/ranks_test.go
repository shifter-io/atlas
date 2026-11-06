package build

import (
	"encoding/json"
	"math/rand"
	"sort"
	"strings"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/Zeus-Development/proxy-index/api/internal/snap"
)

func rankStr(s string) *string { return &s }

// rankEntry is a measured provider with one product of kind and cohort whose
// windows share each range's reporting period.
func rankEntry(kind, cohort string, window func(rangeID string) snap.Window) *entry {
	product := &snap.Product{Kind: kind, Cohort: cohort, ProductID: 7, Windows: map[string]snap.Window{}}
	for _, r := range Ranges {
		w := window(r.ID)
		w.Period = snap.Period{Start: "start-" + r.ID, End: "end", Hours: 24}
		product.Windows[r.ID] = w
	}
	return &entry{P: &model.Provider{Source: model.SourceMeasured}, Snap: map[string]*snap.Product{kind: product}}
}

func withLatency(w snap.Window, p50 Num) snap.Window {
	w.Targets = map[string]snap.TargetResult{latencyTarget: {Metrics: snap.Metrics{LatencyMs: snap.Latency{P50: p50}}}}
	return w
}

// seededWindow is a window with random coverage, latency and place rows: one
// row per place, values drawn with ties and with missing, zero and negative
// ones.
func seededWindow(rng *rand.Rand) snap.Window {
	value := func(max int) Num {
		switch rng.Intn(6) {
		case 0:
			return nan
		case 1:
			return 0
		}
		return Num(rng.Intn(max) + 1)
	}
	latency := func() Num {
		if rng.Intn(6) == 0 {
			return -1
		}
		return value(5) * 100
	}
	w := withLatency(snap.Window{Pool: snap.Pool{UniqueIps: value(4) * 1000, Countries: value(4), Regions: value(4),
		Cities: value(4), Asns: value(4)}}, latency())
	w.Breakdowns = map[string][]snap.BreakdownRow{}
	for _, cc := range []string{"RO", "BG", "DE"} {
		if rng.Intn(4) == 0 {
			continue
		}
		row := func() snap.BreakdownRow {
			return snap.BreakdownRow{CountryCode: rankStr(cc), RegionCode: rankStr(cc + "-1"), Region: rankStr("Region " + cc),
				City: rankStr("City " + cc), Asn: Num(len(cc)*1000 + int(cc[0])), Ips: value(3), Successes: value(2), P50: latency()}
		}
		for _, dimension := range placeDimensions {
			w.Breakdowns[dimension] = append(w.Breakdowns[dimension], row())
		}
	}
	return w
}

func sameRank(a, b model.Rank) bool {
	return a.Peers == b.Peers && (a.Rank == b.Rank || !a.Rank.OK() && !b.Rank.OK())
}

func TestTileRanksMatchADirectRanking(t *testing.T) {
	t.Parallel()
	rng := rand.New(rand.NewSource(2))
	var entries []*entry
	for i := 0; i < 10; i++ {
		entries = append(entries, rankEntry("residential", "pool", func(string) snap.Window { return seededWindow(rng) }))
	}
	addRanks(entries)
	for _, r := range Ranges {
		value := func(e *entry, tile int) Num { return tileValues(e.Snap["residential"].Window(r.ID))[tile] }
		for tile := 0; tile < tileCount; tile++ {
			var valid []Num
			for _, e := range entries {
				if v := value(e, tile); v.OK() {
					valid = append(valid, v)
				}
			}
			higher := tile != tileLatency
			sort.Slice(valid, func(a, b int) bool { return (valid[a] > valid[b]) == higher && valid[a] != valid[b] })
			for i, e := range entries {
				got := e.P.TileRanks["residential"][r.ID]
				gotRank := [tileCount]model.Rank{got.UniqueIps, got.Countries, got.Regions, got.Cities, got.Asns, got.Latency}[tile]
				want := model.Rank{Rank: nan, Peers: len(valid)}
				if v := value(e, tile); v.OK() {
					want.Rank = Num(sort.Search(len(valid), func(j int) bool { return valid[j] == v || (valid[j] < v) == higher }) + 1)
				}
				if !sameRank(gotRank, want) {
					t.Errorf("%s provider %d tile %d: got %+v, want %+v", r.ID, i, tile, gotRank, want)
				}
			}
		}
	}
}

func TestRankJSON(t *testing.T) {
	t.Parallel()
	b, err := json.Marshal(model.TileRanks{Latency: model.Rank{Rank: 2, Peers: 5}})
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(b), `"latency":{"rank":2,"peers":5}`) {
		t.Errorf("tile ranks: %s", b)
	}
}
