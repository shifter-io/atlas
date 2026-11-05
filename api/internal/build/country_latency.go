package build

import (
	"strings"

	"github.com/Zeus-Development/proxy-index-website/api/internal/model"
	"github.com/Zeus-Development/proxy-index-website/api/internal/snap"
)

type countryLatencyGroup struct {
	kind, cohort, rangeID, start, end, country string
}

// Use country percentiles published by the framework, never a mean of city
// percentiles. Each selected provider/product votes once.
func addCountryLatencyRanks(entries []*entry) {
	groups := map[countryLatencyGroup][]Num{}
	byProduct := map[*snap.Product]map[string]map[string]model.CountryLatencyRank{}
	key := func(kind string, p *snap.Product, rangeID, country string) countryLatencyGroup {
		w := p.Window(rangeID)
		return countryLatencyGroup{kind, p.Cohort, rangeID, w.Period.Start, w.Period.End, country}
	}
	for _, e := range entries {
		e.P.CountryLatencyRanks = map[string]map[string]map[string]model.CountryLatencyRank{}
		for kind, p := range e.Snap {
			windows := map[string]map[string]model.CountryLatencyRank{}
			byProduct[p] = windows
			e.P.CountryLatencyRanks[kind] = windows
			for _, r := range Ranges {
				countries := map[string]model.CountryLatencyRank{}
				windows[r.ID] = countries
				for _, row := range p.Window(r.ID).Breakdowns["country"] {
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
				for country, value := range countries {
					if value.P50.OK() {
						k := key(kind, p, r.ID, country)
						groups[k] = append(groups[k], value.P50)
					}
				}
			}
		}
	}
	for _, e := range entries {
		for kind, p := range e.Snap {
			for rangeID, countries := range byProduct[p] {
				for country, value := range countries {
					peers := groups[key(kind, p, rangeID, country)]
					value.Peers = len(peers)
					if value.P50.OK() {
						value.Rank = 1
						for _, latency := range peers {
							if latency < value.P50 {
								value.Rank++
							}
						}
					}
					countries[country] = value
				}
			}
		}
	}
}
