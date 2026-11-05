package build

import (
	"sort"
	"strings"

	"github.com/Zeus-Development/proxy-index-website/api/internal/model"
	"github.com/Zeus-Development/proxy-index-website/api/internal/snap"
	"github.com/Zeus-Development/proxy-index-website/api/internal/store"
)

// A v4 build (payload v4, contract K11) answers its long place lists from the
// network lists the store holds as compact rows (store.NetList), in place:
// such a list is the lists it is merged from, and its rows are merged, ordered
// and cut only when a page is asked for. The build keeps each list's row
// count and first page (rows, not rendered: QA-1 has about 1,700 lists); the
// server renders a page when first asked and keeps it in its page cache
// (server.PagesLRU). A build before v4 keeps every row of every list as
// before.

// placeIndex is one long place list: before v4 every row in order (all); in a
// v4 build the sources whose rows of dimension it merges, only those in
// country cc when set, each source's rows apart and tagged with its network
// when tagged (a country's lists), with the merged row count and first page.
type placeIndex struct {
	all       []PlaceRow
	srcs      []placeSource
	dimension string
	cc        string
	tagged    bool
	total     int
	first     []PlaceRow
}

// placeSource is one product window's network list: the window's breakdown
// scale (contract K8) and, for a tagged list, its network.
type placeSource struct {
	list                *store.NetList
	scale               Num
	network, name, kind string
}

// netListOf is the network list of a product window of a v4 build, nil when
// the build is older or the window names no list.
func netListOf(row *store.SnapshotRow, p *snap.Product, w snap.Window) *store.NetList {
	if row == nil || row.Networks == nil || !p.ProductID.OK() || !w.Period.Hours.OK() {
		return nil
	}
	return row.Networks[store.PlaceWindow{Product: int64(p.ProductID), Cohort: p.Cohort, Hours: int64(w.Period.Hours)}]
}

// count is the list's row count.
func (x *placeIndex) count() int {
	if x.srcs == nil {
		return len(x.all)
	}
	return x.total
}

// rows is every row of the list that holds every token, in list order: in a
// v4 build merged from the sources as a build before v4 merges a network's
// product groups (mergePlace, placeLess).
func (x *placeIndex) rows(tokens []string) []PlaceRow {
	all := x.all
	if x.srcs != nil {
		all = mergeSources(x.srcs, x.dimension, x.cc, x.tagged)
	}
	if len(tokens) == 0 {
		return all
	}
	kept := []PlaceRow{}
	for _, r := range all {
		if matchesAll(r, tokens) {
			kept = append(kept, r)
		}
	}
	return kept
}

// mergeSources merges the sources' rows of dimension (only those in country
// cc when set) by place key: across sources, or, when apart, within each
// network's run of sources, its rows tagged with its network. Rows are
// ordered by placeLess.
func mergeSources(srcs []placeSource, dimension, cc string, apart bool) []PlaceRow {
	var out []PlaceRow
	merged := map[string]*PlaceRow{}
	for i, s := range srcs {
		for j := range s.list.Len(dimension) {
			r := s.list.Row(dimension, j)
			if cc != "" && (r.Place == nil || strings.ToUpper(strings.TrimSpace(model.SV(r.Place.CountryCode))) != cc) {
				continue
			}
			row, asn := netPlaceRow(r, s.scale)
			mergePlace(merged, dimension, row, asn)
		}
		if apart && (i == len(srcs)-1 || srcs[i+1].network != s.network || srcs[i+1].kind != s.kind) {
			out = append(out, tagged(sortedRows(merged), NetworkRow{Slug: s.network, Name: s.name}, s.kind)...)
			merged = map[string]*PlaceRow{}
		}
	}
	if !apart {
		return sortedRows(merged)
	}
	sort.Slice(out, func(i, j int) bool { return placeLess(out[i], out[j]) })
	return out
}

// netPlaceRow is a compact row as a place row and its ASN, as mergeWindow
// reads a typed row.
func netPlaceRow(r store.NetRow, scale Num) (PlaceRow, Num) {
	row, asn := PlaceRow{IpsSum: scaleBy(scale, counted(r.Ips)), Requests: counted(r.Completed), Successes: counted(r.Successes),
		LatencyP50: counted(int64(r.P50))}, nan
	if p := r.Place; p != nil {
		row.CountryCode, row.Country, row.RegionCode, row.Region, row.City = p.CountryCode, p.Country, p.RegionCode, p.Region, p.City
	}
	if n := r.Network; n != nil {
		row.AsnName = n.Name
		if n.Asn != nil {
			asn = Num(*n.Asn)
		}
	}
	return row, asn
}

// counted is a compact counter as a number: NaN when NULL (-1).
func counted(v int64) Num {
	if v < 0 {
		return nan
	}
	return Num(v)
}

// firstPage is a copy of rows' first page, so that the build does not keep
// the merged list it was cut from.
func firstPage(rows []PlaceRow) []PlaceRow {
	out := make([]PlaceRow, min(len(rows), PageSize))
	copy(out, rows)
	return out
}

// countryLists gathers, per country, range and dimension, the city and
// region rows of every network of a v4 build: the rows a country's list
// counts and the candidates of its first page (each network's first PageSize
// rows in the country; a country's first page is among them).
type countryLists struct {
	srcs  map[placeList][]placeSource
	count map[listKey]int
	cand  map[listKey][]PlaceRow
}

// add adds one network's list of a range and dimension, its rows in list
// order merged from srcs (tagged with the network).
func (c *countryLists) add(key placeList, srcs []placeSource, rows []PlaceRow) {
	c.srcs[key] = append(c.srcs[key], srcs...)
	src, seen := srcs[0], map[string]int{}
	for _, r := range rows {
		cc := model.SV(r.CountryCode)
		k := listKey{"country/" + cc, key.rangeID, key.dimension}
		c.count[k]++
		if seen[cc]++; seen[cc] <= PageSize {
			r.Network, r.NetworkName, r.Kind = src.network, src.name, src.kind
			c.cand[k] = append(c.cand[k], r)
		}
	}
}

// indexes is the gathered lists as place lists.
func (c *countryLists) indexes(into map[listKey]*placeIndex) {
	for k, rows := range c.cand {
		sort.Slice(rows, func(i, j int) bool { return placeLess(rows[i], rows[j]) })
		into[k] = &placeIndex{srcs: c.srcs[placeList{k.rangeID, k.dimension}], dimension: k.dimension, cc: k.scope[len("country/"):],
			tagged: true, total: c.count[k], first: firstPage(rows)}
	}
}
