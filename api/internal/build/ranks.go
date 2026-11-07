package build

import (
	"strconv"
	"strings"

	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/Zeus-Development/proxy-index/api/internal/snap"
)

// rankGroup is who a product window is ranked against: measured products of
// the same category and cohort over the same reporting period (as
// countryLatencyGroup, without the country).
type rankGroup struct {
	kind, cohort, rangeID, start, end string
}

// Tile order in tileValues: coverage counts (higher is better), then latency.
const (
	tileUniqueIps = iota
	tileCountries
	tileRegions
	tileCities
	tileAsns
	tileLatency
	tileCount
)

// placeDimensions are the place lists of a network's answers (places.go).
var placeDimensions = [...]string{"country", "region", "city", "asn"}

// rankedWindow is one measured product window waiting for its ranks.
type rankedWindow struct {
	group rankGroup
	tiles [tileCount]Num
	setT  func(model.TileRanks)
}

// addRanks sets every measured provider's TileRanks once per build.
func addRanks(entries []*entry) {
	var windows []rankedWindow
	tilePeers := map[rankGroup]*[tileCount][]Num{}
	for _, e := range entries {
		windows = append(windows, measuredWindows(e)...)
	}
	for _, w := range windows {
		tiles := tilePeers[w.group]
		if tiles == nil {
			tiles = &[tileCount][]Num{}
			tilePeers[w.group] = tiles
		}
		for i, v := range w.tiles {
			if v.OK() {
				tiles[i] = append(tiles[i], v)
			}
		}
	}
	for _, w := range windows {
		w.setT(tileRanks(w.tiles, tilePeers[w.group]))
	}
}

// measuredWindows collects an entry's product windows and gives it empty
// rank maps for them to fill.
func measuredWindows(e *entry) []rankedWindow {
	var out []rankedWindow
	e.P.TileRanks = map[string]map[string]model.TileRanks{}
	for kind, p := range e.Snap {
		tiles := map[string]model.TileRanks{}
		e.P.TileRanks[kind] = tiles
		for _, r := range Ranges {
			w := p.Window(r.ID)
			out = append(out, rankedWindow{
				group: rankGroup{kind, p.Cohort, r.ID, w.Period.Start, w.Period.End},
				tiles: tileValues(w),
				setT:  func(v model.TileRanks) { tiles[r.ID] = v },
			})
		}
	}
	return out
}

// tileValues is a window's coverage counts (counted when above zero) and
// headline latency p50 (counted when not negative).
func tileValues(w snap.Window) [tileCount]Num {
	positive := func(v Num) Num {
		if v.OK() && v > 0 {
			return v
		}
		return nan
	}
	lm, _ := latencyMetrics(w)
	latency := headlineLatency(lm).P50
	if !latency.OK() || latency < 0 {
		latency = nan
	}
	return [tileCount]Num{positive(ipsOf(w)), positive(w.Pool.Countries), positive(w.Pool.Regions),
		positive(w.Pool.Cities), positive(w.Pool.Asns), latency}
}

func tileRanks(v [tileCount]Num, peers *[tileCount][]Num) model.TileRanks {
	return model.TileRanks{
		UniqueIps: rankOf(v[tileUniqueIps], peers[tileUniqueIps], true),
		Countries: rankOf(v[tileCountries], peers[tileCountries], true),
		Regions:   rankOf(v[tileRegions], peers[tileRegions], true),
		Cities:    rankOf(v[tileCities], peers[tileCities], true),
		Asns:      rankOf(v[tileAsns], peers[tileAsns], true),
		Latency:   rankOf(v[tileLatency], peers[tileLatency], false),
	}
}

// rankOf is v's competition rank among peers (1 + peers strictly better);
// NaN when v is missing.
func rankOf(v Num, peers []Num, higherIsBetter bool) model.Rank {
	r := model.Rank{Rank: nan, Peers: len(peers)}
	if !v.OK() {
		return r
	}
	r.Rank = 1
	for _, peer := range peers {
		if (higherIsBetter && peer > v) || (!higherIsBetter && peer < v) {
			r.Rank++
		}
	}
	return r
}

// placeKeyOf is a row's key in a network's place lists: the country code
// ("US"), region ("US|CA|California"), city ("US|CA|California|Los Angeles")
// or ASN ("13335"), region and city keys using the row's region_code and
// region as published, empty when null; false for a row without a country
// (or, for an ASN row, without an ASN).
func placeKeyOf(dimension string, cc, regionCode, region, city *string, asn Num) (string, bool) {
	country := strings.ToUpper(strings.TrimSpace(model.SV(cc)))
	switch dimension {
	case "country":
		return country, country != ""
	case "region":
		return country + "|" + model.SV(regionCode) + "|" + model.SV(region), country != ""
	case "city":
		return country + "|" + model.SV(regionCode) + "|" + model.SV(region) + "|" + model.SV(city), country != ""
	case "asn":
		return strconv.FormatInt(int64(asn), 10), asn.OK()
	}
	return "", false
}
