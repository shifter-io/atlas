package build

import (
	"strings"

	"github.com/shifter-io/atlas/api/internal/model"
	"github.com/shifter-io/atlas/api/internal/snap"
)

type countryLatencyGroup struct {
	kind, cohort, rangeID, start, end, country string
}

// Use country percentiles published by the framework, never a mean of city
// percentiles. Each selected provider/product votes once.
func addCountryLatencyRanks(entries []*entry) {
	groups := map[countryLatencyGroup][]Num{}
	byProduct := map[*snap.Product]map[string]map[string]model.CountryLatencyRank{}
	for _, e := range entries {
		e.P.CountryLatencyRanks = map[string]map[string]map[string]model.CountryLatencyRank{}
		for kind, p := range e.Snap {
			windows := countryLatencyWindows(kind, p, groups)
			byProduct[p] = windows
			e.P.CountryLatencyRanks[kind] = windows
		}
	}
	for _, e := range entries {
		for kind, p := range e.Snap {
			rankCountryLatencies(kind, p, byProduct[p], groups)
		}
	}
}

// countryLatencyKey is the group a product's country latency in a range
// competes in.
func countryLatencyKey(kind string, p *snap.Product, rangeID, country string) countryLatencyGroup {
	w := p.Window(rangeID)
	return countryLatencyGroup{kind, p.Cohort, rangeID, w.Period.Start, w.Period.End, country}
}

// countryLatencyWindows is p's country latencies per range, unranked, each
// latency also added to its group.
func countryLatencyWindows(kind string, p *snap.Product, groups map[countryLatencyGroup][]Num) map[string]map[string]model.CountryLatencyRank {
	windows := map[string]map[string]model.CountryLatencyRank{}
	for _, r := range Ranges {
		countries := countryLatencies(p.Window(r.ID).Breakdowns["country"])
		windows[r.ID] = countries
		for country, value := range countries {
			if value.P50.OK() {
				k := countryLatencyKey(kind, p, r.ID, country)
				groups[k] = append(groups[k], value.P50)
			}
		}
	}
	return windows
}

// countryLatencies are a country breakdown's median latencies by country
// code, N/A without successes, rank unset.
func countryLatencies(rows []snap.BreakdownRow) map[string]model.CountryLatencyRank {
	countries := map[string]model.CountryLatencyRank{}
	for _, row := range rows {
		country := strings.ToUpper(strings.TrimSpace(model.SV(row.CountryCode)))
		if country == "" {
			continue
		}
		latency := nan
		if row.P50.OK() && row.P50 >= 0 && row.Successes > 0 {
			latency = row.P50
		}
		countries[country] = model.CountryLatencyRank{P50: latency, Rank: nan}
	}
	return countries
}

// rankCountryLatencies sets each country latency's peer count and rank
// in its group.
func rankCountryLatencies(kind string, p *snap.Product, windows map[string]map[string]model.CountryLatencyRank, groups map[countryLatencyGroup][]Num) {
	for rangeID, countries := range windows {
		for country, value := range countries {
			peers := groups[countryLatencyKey(kind, p, rangeID, country)]
			value.Peers = len(peers)
			if value.P50.OK() {
				value.Rank = latencyRank(value.P50, peers)
			}
			countries[country] = value
		}
	}
}

// latencyRank is 1 + the number of peers faster than latency.
func latencyRank(latency Num, peers []Num) Num {
	rank := Num(1)
	for _, peer := range peers {
		if peer < latency {
			rank++
		}
	}
	return rank
}
