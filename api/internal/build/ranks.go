package build

import (
	"strconv"
	"strings"

	"github.com/Zeus-Development/proxy-index-website/api/internal/model"
	"github.com/Zeus-Development/proxy-index-website/api/internal/snap"
	"github.com/Zeus-Development/proxy-index-website/api/internal/store"
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

var placeDimensions = [...]string{"country", "region", "city", "asn"}

type placeKey struct{ dimension, key string }

// placeValue is a product window's IPs and latency in one place; a missing
// value is NaN.
type placeValue struct{ ips, latency Num }

// rankedWindow is one measured product window waiting for its ranks.
type rankedWindow struct {
	group  rankGroup
	tiles  [tileCount]Num
	places map[placeKey]placeValue
	setT   func(model.TileRanks)
	setP   func(model.PlaceRanks)
}

// addRanks sets every measured provider's TileRanks and PlaceRanks once per
// build. A place list the snapshot capped is read whole from the build's typed
// rows (store.SnapshotRow.Places).
func addRanks(entries []*entry, s *store.SnapshotRow) {
	var windows []rankedWindow
	tilePeers := map[rankGroup]*[tileCount][]Num{}
	placePeers := map[rankGroup]map[placeKey]*[2][]Num{}
	for _, e := range entries {
		windows = append(windows, measuredWindows(e, s)...)
	}
	for _, w := range windows {
		tiles := tilePeers[w.group]
		if tiles == nil {
			tiles = &[tileCount][]Num{}
			tilePeers[w.group] = tiles
			placePeers[w.group] = map[placeKey]*[2][]Num{}
		}
		for i, v := range w.tiles {
			if v.OK() {
				tiles[i] = append(tiles[i], v)
			}
		}
		for k, v := range w.places {
			peers := placePeers[w.group][k]
			if peers == nil {
				peers = &[2][]Num{}
				placePeers[w.group][k] = peers
			}
			for i, x := range [2]Num{v.ips, v.latency} {
				if x.OK() {
					peers[i] = append(peers[i], x)
				}
			}
		}
	}
	for _, w := range windows {
		w.setT(tileRanks(w.tiles, tilePeers[w.group]))
		w.setP(placeRanks(w.places, placePeers[w.group]))
	}
}

// measuredWindows collects an entry's product windows and gives it empty
// rank maps for them to fill.
func measuredWindows(e *entry, s *store.SnapshotRow) []rankedWindow {
	var out []rankedWindow
	e.P.TileRanks = map[string]map[string]model.TileRanks{}
	e.P.PlaceRanks = map[string]map[string]model.PlaceRanks{}
	for kind, p := range e.Snap {
		tiles := map[string]model.TileRanks{}
		places := map[string]model.PlaceRanks{}
		e.P.TileRanks[kind], e.P.PlaceRanks[kind] = tiles, places
		for _, r := range Ranges {
			w := p.Window(r.ID)
			out = append(out, rankedWindow{
				group:  rankGroup{kind, p.Cohort, r.ID, w.Period.Start, w.Period.End},
				tiles:  tileValues(w),
				places: placeValues(s, p, w),
				setT:   func(v model.TileRanks) { tiles[r.ID] = v },
				setP:   func(v model.PlaceRanks) { places[r.ID] = v },
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

func placeRanks(places map[placeKey]placeValue, peers map[placeKey]*[2][]Num) model.PlaceRanks {
	out := model.PlaceRanks{Countries: map[string]model.PlaceRank{}, Regions: map[string]model.PlaceRank{},
		Cities: map[string]model.PlaceRank{}, Asns: map[string]model.PlaceRank{}}
	byDimension := map[string]map[string]model.PlaceRank{
		"country": out.Countries, "region": out.Regions, "city": out.Cities, "asn": out.Asns}
	for k, v := range places {
		p := peers[k]
		byDimension[k.dimension][k.key] = model.PlaceRank{Ips: rankOf(v.ips, p[0], true), Latency: rankOf(v.latency, p[1], false)}
	}
	return out
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

// placeValues is a window's IPs (counted when above zero) and latency p50
// (counted when not negative and with successes) per place, from every row of
// each list. Rows sharing a key add their IPs and keep the p50 of the row with
// most successes.
func placeValues(s *store.SnapshotRow, p *snap.Product, w snap.Window) map[placeKey]placeValue {
	out := map[placeKey]placeValue{}
	most := map[placeKey]Num{}
	add := func(dimension string, cc, regionCode, region, city *string, asn, ips, successes, p50 Num) {
		key, ok := placeKeyOf(dimension, cc, regionCode, region, city, asn)
		if !ok {
			return
		}
		k := placeKey{dimension, key}
		v, seen := out[k]
		if !seen {
			v = placeValue{ips: nan, latency: nan}
		}
		if ips.OK() && ips > 0 {
			if v.ips.OK() {
				ips += v.ips
			}
			v.ips = ips
		}
		if p50.OK() && p50 >= 0 && successes.OK() && successes > most[k] {
			v.latency, most[k] = p50, successes
		}
		out[k] = v
	}
	typed := capturedPlaceRows(s, p, w)
	for _, dimension := range placeDimensions {
		if rows, ok := typed[dimension]; ok {
			for i := range rows {
				r := rows[i].PlaceRow(dimension)
				add(dimension, r.CountryCode, r.RegionCode, r.Region, r.City, Num(r.Asn), scaledIps(w, Num(r.Ips)), Num(r.Successes), Num(r.P50))
			}
			continue
		}
		for _, r := range w.Breakdowns[dimension] {
			add(dimension, r.CountryCode, r.RegionCode, r.Region, r.City, r.Asn, scaledIps(w, r.Ips), r.Successes, r.P50)
		}
	}
	return out
}

// capturedPlaceRows is, per place list the snapshot capped, the build's typed
// rows of that list when their count is the list's recorded full count (as
// store/longtail.go restores lists). Other lists are used as published.
func capturedPlaceRows(s *store.SnapshotRow, p *snap.Product, w snap.Window) map[string][]store.ListRow {
	if s == nil || !p.ProductID.OK() || !w.Period.Hours.OK() {
		return nil
	}
	l := s.Places[store.PlaceWindow{Product: int64(p.ProductID), Cohort: p.Cohort, Hours: int64(w.Period.Hours)}]
	out := map[string][]store.ListRow{}
	for _, dimension := range placeDimensions {
		list := l.Rows(dimension)
		n, recorded := w.BreakdownEntries[dimension]
		if recorded && len(w.Breakdowns[dimension]) < n && len(list) == n {
			out[dimension] = list
		}
	}
	return out
}

// placeKeyOf is the PlaceRanks key of a row (see model.PlaceRanks); false
// for a row without a country (or, for an ASN row, without an ASN).
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
