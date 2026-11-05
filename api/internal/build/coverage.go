package build

import (
	"encoding/json"
	"strings"

	"github.com/Zeus-Development/proxy-index-website/api/internal/model"
	"github.com/Zeus-Development/proxy-index-website/api/internal/store"
)

// coverageList is one network window's City x ASN rows as the Coverage answer
// reads them (contract K11): its network list, the window's breakdown scale
// and the category's country latency ranks of that range.
type coverageList struct {
	list  *store.NetList
	scale Num
	ranks map[string]model.CountryLatencyRank
	first *CoveragePage
}

// CoverageRow is one row of /v1/networks/{slug}/coverage: a coverage evidence
// row with its country's latency rank among the category's networks, written
// as [countryCode, country, region, city, asn, asnName, usageType, ips,
// latencyP50, countryLatencyRank, countryLatencyPeers].
type CoverageRow struct {
	model.CoverageRow
	Rank  Num
	Peers int
}

func (r CoverageRow) MarshalJSON() ([]byte, error) {
	c := r.CoverageRow
	return json.Marshal([...]any{c.CountryCode, c.Country, c.Region, c.City, c.Asn, c.AsnName, c.UsageType, c.Ips, c.LatencyP50, r.Rank, r.Peers})
}

// CoverageTotals are a search's summed IPs and its distinct countries, cities
// and ASNs over every matching row.
type CoverageTotals struct {
	Ips       Num `json:"ips"`
	Countries int `json:"countries"`
	Cities    int `json:"cities"`
	Asns      int `json:"asns"`
}

// CoveragePage is one page of /v1/networks/{slug}/coverage; Totals only with
// a query.
type CoveragePage struct {
	GeneratedAt string          `json:"generatedAt"`
	Network     string          `json:"network"`
	Kind        string          `json:"kind"`
	Range       string          `json:"range"`
	Query       string          `json:"query"`
	Page        int             `json:"page"`
	PageSize    int             `json:"pageSize"`
	Total       int             `json:"total"`
	Pages       int             `json:"pages"`
	Totals      *CoverageTotals `json:"totals,omitempty"`
	Rows        []CoverageRow   `json:"rows"`
}

// addCoverage keeps, in a v4 build, the network's City x ASN list of each
// range for its Coverage answer: its headline group's list, as the coverage
// evidence reads it.
func (pg *Pages) addCoverage(e *entry, kind string, row *store.SnapshotRow) {
	p := e.Snap[kind]
	if !pg.v4 || p == nil {
		return
	}
	for _, r := range Ranges {
		w := p.Window(r.ID)
		if l := netListOf(row, p, w); l != nil {
			k := listKey{e.P.Slug + "/" + kind, r.ID, "coverage"}
			pg.coverage[k] = &coverageList{list: l, scale: breakdownScale(w), ranks: e.P.CountryLatencyRanks[kind][r.ID]}
			pg.coverage[k].first, _ = pg.Coverage("", k.scope, k.rangeID, "", 1)
		}
	}
}

// Coverage is one page of a network's Coverage answer: its City x ASN rows
// with IPs, most IPs first, those holding every word of the query; ok is
// false when the build has no such list. The build holds the first page
// without a query.
func (pg *Pages) Coverage(generatedAt, scope, rangeID, query string, page int) (*CoveragePage, bool) {
	c := pg.coverage[listKey{scope, rangeID, "coverage"}]
	if c == nil {
		return nil, false
	}
	tokens := Tokens(query)
	if c.first != nil && page == 1 && len(tokens) == 0 {
		out := *c.first
		out.GeneratedAt = generatedAt
		return &out, true
	}
	network, kind, _ := strings.Cut(scope, "/")
	out := &CoveragePage{GeneratedAt: generatedAt, Network: network, Kind: kind, Range: rangeID, Query: strings.Join(tokens, " "),
		Page: page, PageSize: PageSize, Rows: []CoverageRow{}}
	matched := []int32{}
	for _, i := range c.list.Match("city_asn", out.Query, c.list.ByIps("city_asn")) {
		if c.list.Row("city_asn", int(i)).Ips > 0 {
			matched = append(matched, i)
		}
	}
	out.Total, out.Pages = len(matched), (len(matched)+PageSize-1)/PageSize
	if len(tokens) > 0 {
		t := c.list.Totals("city_asn", matched)
		out.Totals = &CoverageTotals{Ips: 0, Countries: t.Countries, Cities: t.Cities, Asns: t.Asns}
		for _, i := range matched {
			out.Totals.Ips += c.row(i).Ips
		}
	}
	if start := (page - 1) * PageSize; start < len(matched) {
		for _, i := range matched[start:min(len(matched), start+PageSize)] {
			out.Rows = append(out.Rows, c.row(i))
		}
	}
	return out, true
}

// row is the list's City x ASN row i as the answer writes it.
func (c *coverageList) row(i int32) CoverageRow {
	r := c.list.Row("city_asn", int(i))
	out := CoverageRow{CoverageRow: coverageRow(r, c.scale), Rank: nan}
	if r.Place != nil {
		if rank, ok := c.ranks[strings.ToUpper(strings.TrimSpace(model.SV(r.Place.CountryCode)))]; ok {
			out.Rank, out.Peers = rank.Rank, rank.Peers
		}
	}
	return out
}

// coverageRow is a compact City x ASN row as a coverage evidence row.
func coverageRow(r store.NetRow, scale Num) model.CoverageRow {
	out := model.CoverageRow{Asn: nan, Ips: scaleBy(scale, counted(r.Ips)), LatencyP50: counted(int64(r.P50))}
	if p := r.Place; p != nil {
		out.CountryCode, out.Country, out.Region, out.City = p.CountryCode, p.Country, p.Region, p.City
	}
	if n := r.Network; n != nil {
		out.AsnName, out.UsageType = n.Name, n.UsageType
		if n.Asn != nil {
			out.Asn = Num(*n.Asn)
		}
	}
	return out
}
