package build

import (
	"context"
	"encoding/json"
	"strings"

	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/store"
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
// row with its country's latency rank among the category's networks and its
// own IP rank among the networks at its City x ASN (calculated v15), written
// as an array of the values coverageColumns names.
type CoverageRow struct {
	model.CoverageRow
	Rank              Num
	Peers             int
	IpsRank, IpsPeers Num
}

// coverageColumns name a Coverage row's positions (the answer's columns).
// The country's name (1) stays until the website reads the rows by their
// columns (#992); the dataset file's rows have none (cells).
var coverageColumns = []string{"countryCode", "country", "region", "city", "asn", "asnName", "usageType", "ips", "latencyP50",
	"latencyRank", "latencyPeers", "ipsRank", "ipsPeers"}

func (r CoverageRow) MarshalJSON() ([]byte, error) { return json.Marshal(r.cells(true)) }

// cells is the row's values in coverageColumns order, without the country's
// name unless country is set.
func (r CoverageRow) cells(country bool) []any {
	c := r.CoverageRow
	out := []any{c.CountryCode, c.Country, c.Region, c.City, c.Asn, c.AsnName, c.UsageType, c.Ips, c.LatencyP50, r.Rank, r.Peers,
		r.IpsRank, r.IpsPeers}
	if !country {
		out = append(out[:1], out[2:]...)
	}
	return out
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
// a filter. Columns name the rows' positions.
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
	Columns     []string        `json:"columns"`
	Rows        []CoverageRow   `json:"rows"`
}

// addCoverage keeps the network's City x ASN list of each
// range for its Coverage answer: its headline group's list, as the coverage
// evidence reads it.
func (pg *Pages) addCoverage(e *entry, kind string, nr *netRead) {
	p := e.Snap[kind]
	if p == nil || nr.err != nil {
		return
	}
	for _, r := range Ranges {
		w := p.Window(r.ID)
		if l := netListOf(nr.row, p, w); l != nil {
			k := listKey{e.P.Slug + "/" + kind, r.ID, "coverage"}
			pg.coverage[k] = &coverageList{list: l, scale: breakdownScale(w), ranks: e.P.CountryLatencyRanks[kind][r.ID]}
			if pg.coverage[k].first, _, nr.err = pg.Coverage(nr.ctx, "", k.scope, k.rangeID, ListFilter{}, 1); nr.err != nil {
				return
			}
		}
	}
}

// Coverage is one page of a network's Coverage answer: its City x ASN rows
// with IPs, most IPs first, those the filter keeps (ListFilter); ok is
// false when the build has no such list. The build holds the first page
// without a filter; another page reads the list's rows (from the database
// unless cached).
func (pg *Pages) Coverage(ctx context.Context, generatedAt, scope, rangeID string, f ListFilter, page int) (*CoveragePage, bool, error) {
	c := pg.coverage[listKey{scope, rangeID, "coverage"}]
	if c == nil {
		return nil, false, nil
	}
	tokens := Tokens(f.Query)
	if c.first != nil && page == 1 && len(tokens) == 0 && f.Country == "" {
		out := *c.first
		out.GeneratedAt = generatedAt
		return &out, true, nil
	}
	rows, err := store.RowsOf(ctx, c.list, "city_asn")
	if err != nil {
		return nil, false, err
	}
	network, kind, _ := strings.Cut(scope, "/")
	out := &CoveragePage{GeneratedAt: generatedAt, Network: network, Kind: kind, Range: rangeID, Query: strings.Join(tokens, " "),
		Page: page, PageSize: PageSize, Columns: coverageColumns, Rows: []CoverageRow{}}
	found := rows.InCountry(rows.Match(out.Query, nil), f.Country)
	matched := found[:0]
	for _, i := range found {
		if rows.Ips(int(i)) > 0 {
			matched = append(matched, i)
		}
	}
	out.Total, out.Pages = len(matched), (len(matched)+PageSize-1)/PageSize
	if len(tokens) > 0 || f.Country != "" {
		// In position order, as the rows lie in memory: each row's IPs as its
		// answer row scales them (coverageRow), whole numbers whose sum is the
		// same in any order.
		t := rows.Totals(matched)
		out.Totals = &CoverageTotals{Ips: 0, Countries: t.Countries, Cities: t.Cities, Asns: t.Asns}
		for _, i := range matched {
			out.Totals.Ips += scaleBy(c.scale, counted(rows.Ips(int(i))))
		}
	}
	top := rows.TopByIps(matched, page*PageSize) // this page's rows and those before it
	if start := (page - 1) * PageSize; start < len(top) {
		for _, i := range top[start:] {
			out.Rows = append(out.Rows, c.row(rows, i))
		}
	}
	return out, true, nil
}

// row is City x ASN row i of the list's rows as the answer writes it.
func (c *coverageList) row(rows *store.NetRows, i int32) CoverageRow {
	r := rows.Row(int(i))
	out := CoverageRow{CoverageRow: coverageRow(r, c.scale), Rank: nan, IpsRank: nan, IpsPeers: nan}
	if rank := ipsRankOf(r); rank != nil {
		out.IpsRank, out.IpsPeers = rank.Rank, Num(rank.Peers)
	}
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
