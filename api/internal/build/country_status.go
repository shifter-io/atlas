package build

import (
	"sort"

	"github.com/Zeus-Development/atlas/api/internal/snap"
)

// A network answer's requestedCountryStatus (COUNTRY-MIN-IPS-1): each requested
// country's status from the network summaries' criteria.country_status.

// statusRank orders the country_status codes: a country counts for a network
// when it counts for one of its groups.
var statusRank = map[string]int{"counts": 0, "too_few_ips": 1, "no_daily_test": 2}

// requestedCountryStatus is each requested country's status over the groups,
// sorted by country: the best status (statusRank) and, of the groups with it,
// the most unique IPs found.
func requestedCountryStatus(groups []*snap.Product) []CountryStatusRow {
	best := map[string]CountryStatusRow{}
	for _, p := range groups {
		for _, s := range p.Criteria.CountryStatus {
			cur, seen := best[s.Country]
			better := !seen || statusRank[s.Status] < statusRank[cur.Status]
			more := seen && s.Status == cur.Status && s.UniqueIps.OK() && (!cur.UniqueIps.OK() || s.UniqueIps > cur.UniqueIps)
			if better || more {
				best[s.Country] = CountryStatusRow{Country: s.Country, Status: s.Status, UniqueIps: s.UniqueIps}
			}
		}
	}
	out := make([]CountryStatusRow, 0, len(best))
	for _, row := range best {
		out = append(out, row)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Country < out[j].Country })
	return out
}
