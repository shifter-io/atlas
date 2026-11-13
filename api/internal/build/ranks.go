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

// RangeRanks is a network's competition rank among the networks of its kind
// in one range (its rankings rows, /v1/rankings?range=&kind=): success rate,
// uptime and price higher or lower is better, as the site reads them. ValueRank
// ranks cost per 1,000 successes (the row's published valueRank and its count
// when it has one), ListRate the list $/GB (both lower first).
type RangeRanks struct {
	SuccessRate        model.Rank `json:"successRate"`
	UptimePct          model.Rank `json:"uptimePct"`
	DatacenterAsnShare model.Rank `json:"datacenterAsnShare"`
	ValueRank          model.Rank `json:"valueRank"`
	ListRate           model.Rank `json:"listRate"`
}

// Alternative is provider-view.ts cheaperAndBetter[0]: the first network of
// the kind's ranking whose all-targets bill (costPer1kAllTargets) is below
// this one's with a success rate at most AlternativeSlack below it. CheaperBy
// is 1 − its bill ÷ this network's (HTTP-COST-RETIRE-1: the plain-HTTP cost
// before).
type Alternative struct {
	Slug        string `json:"slug"`
	Name        string `json:"name"`
	SuccessRate Num    `json:"successRate"`
	// SuccessScore is the alternative's overall success score in the range
	// (its ranking row's, without targets and weights); null without one.
	SuccessScore *model.MetricScore `json:"successScore"`
	LatencyP50   Num                `json:"latencyP50"`
	CheaperBy    Num                `json:"cheaperBy"`
}

// AlternativeSlack is how far below a network's success rate an alternative's
// may be (provider-view.ts: 2 points).
const AlternativeSlack = 0.02

// rankedFigures are the figures RangeRanks ranks, in its field order, and
// whether a higher value is better.
var rankedFigures = [...]struct {
	of     func(RankingRow) Num
	higher bool
}{
	{func(r RankingRow) Num { return r.SuccessRate }, true},
	{func(r RankingRow) Num { return r.UptimePct }, true},
	{func(r RankingRow) Num { return r.DatacenterAsnShare }, false},
	{func(r RankingRow) Num { return r.CostPer1k }, false},
	{func(r RankingRow) Num { return r.ListRate }, false},
}

func noRanks() RangeRanks {
	none := model.Rank{Rank: nan}
	return RangeRanks{none, none, none, none, none}
}

// addRangeRanks sets every network answer's per-range ranks and alternative
// from its kind's ranking of that range, once per build.
func (pg *Pages) addRangeRanks() {
	for key, ranking := range pg.RankingsByKind {
		rangeID, kind, _ := strings.Cut(key, "/")
		ranks := rowRanks(ranking.Networks)
		for _, row := range ranking.Networks {
			ans := pg.Network[row.Slug+"/"+kind]
			if ans == nil || ans.Stats[rangeID] == nil {
				continue
			}
			ans.Stats[rangeID].Ranks, ans.Stats[rangeID].Alternative = ranks[row.Slug], alternative(row, ranking.Networks)
		}
	}
}

// rowRanks ranks every row's figures among rows, by slug.
func rowRanks(rows []RankingRow) map[string]RangeRanks {
	var peers [len(rankedFigures)][]Num
	for _, r := range rows {
		for i, f := range rankedFigures {
			if v := f.of(r); v.OK() {
				peers[i] = append(peers[i], v)
			}
		}
	}
	out := make(map[string]RangeRanks, len(rows))
	for _, r := range rows {
		var got [len(rankedFigures)]model.Rank
		for i, f := range rankedFigures {
			got[i] = rankOf(f.of(r), peers[i], f.higher)
		}
		if r.valueRanked > 0 { // the published value rank (valueRankOf)
			got[3] = model.Rank{Rank: nan, Peers: r.valueRanked}
			if r.ValueRank != nil {
				got[3].Rank = Num(*r.ValueRank)
			}
		}
		out[r.Slug] = RangeRanks{got[0], got[1], got[2], got[3], got[4]}
	}
	return out
}

// alternative is own's Alternative among rows (in ranking order); nil when
// no network qualifies or own has no cost or success rate.
func alternative(own RankingRow, rows []RankingRow) *Alternative {
	if !own.CostPer1kAllTargets.OK() || !own.SuccessRate.OK() {
		return nil
	}
	for _, r := range rows {
		if r.Slug == own.Slug || !r.CostPer1kAllTargets.OK() || !r.SuccessRate.OK() {
			continue
		}
		if r.CostPer1kAllTargets < own.CostPer1kAllTargets && r.SuccessRate >= own.SuccessRate-AlternativeSlack {
			alt := &Alternative{Slug: r.Slug, Name: r.Name, SuccessRate: r.SuccessRate, LatencyP50: r.LatencyP50,
				CheaperBy: 1 - r.CostPer1kAllTargets/own.CostPer1kAllTargets}
			if r.SuccessScore != nil {
				score := r.SuccessScore.MetricScore
				alt.SuccessScore = &score
			}
			return alt
		}
	}
	return nil
}
