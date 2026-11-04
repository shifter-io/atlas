package build

import (
	"encoding/json"
	"fmt"
	"math/rand"
	"sort"
	"strconv"
	"strings"
	"testing"

	"github.com/Zeus-Development/proxy-index-website/api/internal/model"
	"github.com/Zeus-Development/proxy-index-website/api/internal/snap"
	"github.com/Zeus-Development/proxy-index-website/api/internal/store"
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

// directRanks ranks the fixture's rows the way SQL RANK() OVER (PARTITION BY
// kind, cohort, range, period, dimension, place ORDER BY value) does: sort the
// valid values, a row's rank is one plus the position of the first value equal
// to its own.
func directRanks(entries []*entry) map[string]model.PlaceRank {
	type vote struct {
		id    string
		value Num
	}
	partitions := map[string][2][]vote{}
	for i, e := range entries {
		if e.P.Source != model.SourceMeasured {
			continue
		}
		for kind, p := range e.Snap {
			for _, r := range Ranges {
				w := p.Window(r.ID)
				for _, dimension := range placeDimensions {
					for _, row := range w.Breakdowns[dimension] {
						key := model.SV(row.CountryCode)
						if dimension == "asn" {
							key = fmt.Sprint(int64(row.Asn))
						}
						part := strings.Join([]string{kind, p.Cohort, r.ID, w.Period.Start, w.Period.End, dimension, key}, "/")
						votes := partitions[part]
						id := fmt.Sprint(i, "/", kind, "/", r.ID, "/", dimension, "/", key)
						votes[0] = append(votes[0], vote{id, row.Ips})
						latency := nan
						if row.P50 >= 0 && row.Successes > 0 {
							latency = row.P50
						}
						votes[1] = append(votes[1], vote{id, latency})
						partitions[part] = votes
					}
				}
			}
		}
	}
	out := map[string]model.PlaceRank{}
	for _, votes := range partitions {
		ranks := [2]map[string]model.Rank{}
		for metric, list := range votes {
			var valid []Num
			for _, v := range list {
				if v.value.OK() && (metric == 1 || v.value > 0) {
					valid = append(valid, v.value)
				}
			}
			sort.Slice(valid, func(a, b int) bool {
				if metric == 0 {
					return valid[a] > valid[b]
				}
				return valid[a] < valid[b]
			})
			ranks[metric] = map[string]model.Rank{}
			for _, v := range list {
				r := model.Rank{Rank: nan, Peers: len(valid)}
				for i, x := range valid {
					if x == v.value && (metric == 1 || v.value > 0) {
						r.Rank = Num(i + 1)
						break
					}
				}
				ranks[metric][v.id] = r
			}
		}
		for _, v := range votes[0] {
			out[v.id] = model.PlaceRank{Ips: ranks[0][v.id], Latency: ranks[1][v.id]}
		}
	}
	return out
}

func sameRank(a, b model.Rank) bool {
	return a.Peers == b.Peers && (a.Rank == b.Rank || !a.Rank.OK() && !b.Rank.OK())
}

func TestPlaceRanksMatchADirectRanking(t *testing.T) {
	rng := rand.New(rand.NewSource(1))
	var entries []*entry
	for i := 0; i < 12; i++ {
		kind, cohort := "residential", "pool"
		if i == 10 {
			kind = "mobile"
		}
		if i == 11 {
			cohort = "other"
		}
		entries = append(entries, rankEntry(kind, cohort, func(string) snap.Window { return seededWindow(rng) }))
	}
	addRanks(entries, nil)
	want := directRanks(entries)
	ranked := 0
	for id, w := range want {
		parts := strings.Split(id, "/")
		i, err := strconv.Atoi(parts[0])
		if err != nil {
			t.Fatal(err)
		}
		kind, rangeID, dimension, key := parts[1], parts[2], parts[3], parts[4]
		ranks := entries[i].P.PlaceRanks[kind][rangeID]
		lists := map[string]map[string]model.PlaceRank{"country": ranks.Countries, "region": ranks.Regions,
			"city": ranks.Cities, "asn": ranks.Asns}
		var got model.PlaceRank
		for k, v := range lists[dimension] {
			if strings.HasPrefix(k, key) {
				got = v
			}
		}
		if !sameRank(got.Ips, w.Ips) || !sameRank(got.Latency, w.Latency) {
			t.Errorf("%s: got %+v, want %+v", id, got, w)
		}
		if w.Ips.Rank.OK() {
			ranked++
		}
	}
	if ranked < 50 {
		t.Fatalf("fixture ranked only %d rows", ranked)
	}
}

func TestTileRanksMatchADirectRanking(t *testing.T) {
	rng := rand.New(rand.NewSource(2))
	var entries []*entry
	for i := 0; i < 10; i++ {
		entries = append(entries, rankEntry("residential", "pool", func(string) snap.Window { return seededWindow(rng) }))
	}
	addRanks(entries, nil)
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

func TestPlaceRanksMergeKeysAndPreferWholeTypedLists(t *testing.T) {
	city := func(cc, name string, ips, successes, p50 Num) snap.BreakdownRow {
		return snap.BreakdownRow{CountryCode: rankStr(cc), RegionCode: rankStr("B"), Region: rankStr("Bucharest"),
			City: rankStr(name), Ips: ips, Successes: successes, P50: p50}
	}
	merged := rankEntry("residential", "pool", func(string) snap.Window {
		return snap.Window{Breakdowns: map[string][]snap.BreakdownRow{
			"city": {city(" ro ", "Bucharest", 10, 20, 300), city("RO", "Bucharest", 5, 50, 100), city("", "Nowhere", 9, 9, 9)},
			"asn":  {{Asn: 8708, Ips: 4}, {Asn: nan, Ips: 99}},
		}}
	})
	// The snapshot capped the city list to 1 of 2 rows and the ASN list to 1 of 3.
	capped := rankEntry("residential", "pool", func(string) snap.Window {
		return snap.Window{Breakdowns: map[string][]snap.BreakdownRow{"city": {city("RO", "Bucharest", 1, 1, 900)},
			"asn": {{Asn: 8708, Ips: 1}}}, BreakdownEntries: map[string]int{"city": 2, "asn": 3}}
	})
	plain := rankEntry("residential", "pool", func(string) snap.Window {
		return snap.Window{Breakdowns: map[string][]snap.BreakdownRow{"city": {city("RO", "Bucharest", 14, 5, 200)},
			"asn": {{Asn: 8708, Ips: 2}}}}
	})
	cappedProduct := capped.Snap["residential"]
	cappedProduct.ProductID = 8
	bucharest := &store.Place{CountryCode: rankStr("RO"), RegionCode: rankStr("B"), Region: rankStr("Bucharest"), City: rankStr("Bucharest")}
	cluj := &store.Place{CountryCode: rankStr("RO"), RegionCode: rankStr("B"), Region: rankStr("Bucharest"), City: rankStr("Cluj")}
	asn := func(n int64) *store.Network { return &store.Network{Asn: &n} }
	typed := &store.SnapshotRow{Places: map[store.PlaceWindow]*store.List{
		{Product: 8, Cohort: "pool", Hours: 24}: store.NewList(map[string][]store.ListRow{
			"city": {{Place: bucharest, Ips: 20, Completed: 9, Successes: 7, P50: 150},
				{Place: cluj, Ips: 3, Completed: -1, Successes: -1, P50: -1}},
			// Two ASN rows where three are recorded: the published list stays.
			"asn": {{Network: asn(8708), Ips: 50, Successes: -1, P50: -1}, {Network: asn(9050), Ips: 50, Successes: -1, P50: -1}},
		})}}
	addRanks([]*entry{merged, capped, plain}, typed)
	key := "RO|B|Bucharest|Bucharest"
	for _, c := range []struct {
		name      string
		e         *entry
		ips, lat  Num
		ipsPeers  int
		latPeers  int
		dimension string
		key       string
	}{
		// merged: 15 IPs and the p50 of its 50-success row (100).
		{"merged city", merged, 2, 1, 3, 3, "city", key},
		{"typed city", capped, 1, 2, 3, 3, "city", key},
		{"plain city", plain, 3, 3, 3, 3, "city", key},
		{"typed only city", capped, 1, nan, 1, 0, "city", "RO|B|Bucharest|Cluj"},
		{"published asn", capped, 3, nan, 3, 0, "asn", "8708"},
		{"merged asn", merged, 1, nan, 3, 0, "asn", "8708"},
	} {
		ranks := c.e.P.PlaceRanks["residential"]["30d"]
		got := map[string]map[string]model.PlaceRank{"city": ranks.Cities, "asn": ranks.Asns}[c.dimension][c.key]
		want := model.PlaceRank{Ips: model.Rank{Rank: c.ips, Peers: c.ipsPeers}, Latency: model.Rank{Rank: c.lat, Peers: c.latPeers}}
		if !sameRank(got.Ips, want.Ips) || !sameRank(got.Latency, want.Latency) {
			t.Errorf("%s: got %+v, want %+v", c.name, got, want)
		}
	}
	if n := len(merged.P.PlaceRanks["residential"]["30d"].Cities); n != 1 {
		t.Errorf("merged provider has %d city ranks, want 1 (no row without a country)", n)
	}
	if n := len(merged.P.PlaceRanks["residential"]["30d"].Asns); n != 1 {
		t.Errorf("merged provider has %d ASN ranks, want 1 (no row without an ASN)", n)
	}
}

func TestRankJSON(t *testing.T) {
	b, err := json.Marshal(model.PlaceRanks{Countries: map[string]model.PlaceRank{
		"RO": {Ips: model.Rank{Rank: 1, Peers: 3}, Latency: model.Rank{Rank: nan, Peers: 2}}}})
	if err != nil {
		t.Fatal(err)
	}
	if want := `{"countries":{"RO":[1,3,null,2]},"regions":null,"cities":null,"asns":null}`; string(b) != want {
		t.Errorf("got %s, want %s", b, want)
	}
	b, err = json.Marshal(model.TileRanks{Latency: model.Rank{Rank: 2, Peers: 5}})
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(b), `"latency":{"rank":2,"peers":5}`) {
		t.Errorf("tile ranks: %s", b)
	}
}
