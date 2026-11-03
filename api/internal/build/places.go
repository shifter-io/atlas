package build

import (
	"sort"
	"strconv"
	"strings"

	"github.com/Zeus-Development/proxy-index-website/api/internal/model"
	"github.com/Zeus-Development/proxy-index-website/api/internal/snap"
	"github.com/Zeus-Development/proxy-index-website/api/internal/store"
)

// listKey names one precomputed place list: scope is "slug/kind" for a
// network's list or "country/CC" for a country's cities across networks.
type listKey struct{ scope, rangeID, dimension string }

type placeList struct{ rangeID, dimension string }

// networkAnswer sums a network's product groups of one category: its 30-day
// days, and per range its totals and every place list (the top countries go
// into the answer, every list into the pages).
func networkAnswer(generatedAt string, head NetworkRow, kind string, groups []*snap.Product, row *store.SnapshotRow) (*NetworkAnswer, map[placeList][]PlaceRow) {
	ans := &NetworkAnswer{GeneratedAt: generatedAt, Network: head, Kind: kind, ProductGroups: len(groups),
		RequestedCountries: requestedCountries(groups), Days: days(groups), Ranges: map[string]*RangeTotals{}}
	lists := map[placeList][]PlaceRow{}
	for _, r := range Ranges {
		t := &RangeTotals{Requests: 0, Successes: 0, IpsSum: nan, Lists: map[string]int{}}
		merged := map[string]map[string]*PlaceRow{}
		for _, d := range placeDimensions {
			merged[d] = map[string]*PlaceRow{}
		}
		for _, p := range groups {
			w := p.Window(r.ID)
			t.Requests, t.Successes = addNum(t.Requests, w.Metrics.Completed), addNum(t.Successes, w.Metrics.Successes)
			t.IpsSum = addNum(t.IpsSum, w.Pool.UniqueIps)
			mergeWindow(merged, row, p, w)
		}
		if len(groups) == 0 {
			t.Requests, t.Successes = nan, nan
		}
		t.SuccessRate = ratio(t.Successes, t.Requests)
		for _, d := range placeDimensions {
			rows := sortedRows(merged[d])
			lists[placeList{r.ID, d}] = rows
			t.Lists[d] = len(rows)
		}
		countries := lists[placeList{r.ID, "country"}]
		t.ExitCountries = len(countries)
		t.Countries = countries[:min(len(countries), TopCountries)]
		ans.Ranges[r.ID] = t
	}
	return ans, lists
}

func requestedCountries(groups []*snap.Product) []string {
	seen := map[string]bool{}
	out := []string{}
	for _, p := range groups {
		if cc := strings.ToUpper(model.SV(p.Criteria.Country)); cc != "" && !seen[cc] {
			seen[cc] = true
			out = append(out, cc)
		}
	}
	sort.Strings(out)
	return out
}

// days sums the groups' 30-day series (one row per UTC day) by day.
func days(groups []*snap.Product) []DayRow {
	byDay := map[string]*DayRow{}
	for _, p := range groups {
		for _, s := range p.Window("30d").Series {
			day := s.At
			if len(day) >= 10 {
				day = day[:10]
			}
			d := byDay[day]
			if d == nil {
				d = &DayRow{Day: day, Requests: 0, Successes: 0, IpsSum: nan}
				byDay[day] = d
			}
			d.Requests, d.Successes, d.IpsSum = addNum(d.Requests, s.Completed), addNum(d.Successes, s.Successes), addNum(d.IpsSum, s.UniqueIps)
		}
	}
	out := make([]DayRow, 0, len(byDay))
	for _, d := range byDay {
		d.SuccessRate = ratio(d.Successes, d.Requests)
		out = append(out, *d)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Day < out[j].Day })
	return out
}

// mergeWindow adds a group window's place rows: a list the snapshot capped is
// read whole from the typed rows when they are complete (capturedPlaceRows).
func mergeWindow(merged map[string]map[string]*PlaceRow, row *store.SnapshotRow, p *snap.Product, w snap.Window) {
	typed := capturedPlaceRows(row, p, w)
	for _, d := range placeDimensions {
		if rows, ok := typed[d]; ok {
			for _, r := range rows {
				mergePlace(merged[d], d, PlaceRow{CountryCode: r.CountryCode, Country: r.Country, RegionCode: r.RegionCode,
					Region: r.Region, City: r.City, AsnName: r.Name, IpsSum: Num(r.Ips), Requests: Num(r.Completed),
					Successes: Num(r.Successes), LatencyP50: Num(r.P50)}, Num(r.Asn))
			}
			continue
		}
		for _, r := range w.Breakdowns[d] {
			mergePlace(merged[d], d, PlaceRow{CountryCode: r.CountryCode, Country: r.Country, RegionCode: r.RegionCode,
				Region: r.Region, City: r.City, AsnName: r.Name, IpsSum: r.Ips, Requests: r.Completed, Successes: r.Successes,
				LatencyP50: r.P50}, r.Asn)
		}
	}
}

func mergePlace(into map[string]*PlaceRow, dimension string, r PlaceRow, asn Num) {
	key, ok := placeKeyOf(dimension, r.CountryCode, r.RegionCode, r.Region, r.City, asn)
	if !ok {
		return
	}
	cur := into[key]
	if cur == nil {
		r.Key, r.most = key, nan
		if dimension == "asn" {
			n := int64(asn)
			r.Asn = &n
		} else {
			cc := key[:min(len(key), 2)]
			if i := strings.IndexByte(key, '|'); i >= 0 {
				cc = key[:i]
			}
			r.CountryCode = &cc
		}
		if !r.LatencyP50.OK() || r.LatencyP50 < 0 || !r.Successes.OK() || r.Successes <= 0 {
			r.LatencyP50 = nan
		} else {
			r.most = r.Successes
		}
		into[key] = &r
		return
	}
	cur.IpsSum, cur.Requests = addNum(cur.IpsSum, r.IpsSum), addNum(cur.Requests, r.Requests)
	if r.LatencyP50.OK() && r.LatencyP50 >= 0 && r.Successes.OK() && r.Successes > 0 && (!cur.most.OK() || r.Successes > cur.most) {
		cur.LatencyP50, cur.most = r.LatencyP50, r.Successes
	}
	cur.Successes = addNum(cur.Successes, r.Successes)
}

// sortedRows lists merged rows by IPs (most first), then key.
func sortedRows(m map[string]*PlaceRow) []PlaceRow {
	out := make([]PlaceRow, 0, len(m))
	for _, r := range m {
		r.SuccessRate = ratio(r.Successes, r.Requests)
		out = append(out, *r)
	}
	sort.Slice(out, func(i, j int) bool { return placeLess(out[i], out[j]) })
	return out
}

func placeLess(a, b PlaceRow) bool {
	ai, bi := a.IpsSum, b.IpsSum
	if !ai.OK() {
		ai = -1
	}
	if !bi.OK() {
		bi = -1
	}
	if ai != bi {
		return ai > bi
	}
	if a.Key != b.Key {
		return a.Key < b.Key
	}
	return a.Network+a.Kind < b.Network+b.Kind
}

// addNum adds two counters; a missing value counts as nothing unless both are.
func addNum(a, b Num) Num {
	switch {
	case !b.OK():
		return a
	case !a.OK():
		return b
	}
	return a + b
}

func ratio(a, b Num) Num {
	if !a.OK() || !b.OK() || b <= 0 {
		return nan
	}
	return a / b
}

func tagged(rows []PlaceRow, head NetworkRow, kind string) []PlaceRow {
	out := make([]PlaceRow, len(rows))
	for i, r := range rows {
		r.Network, r.NetworkName, r.Kind = head.Slug, head.Name, kind
		out[i] = r
	}
	return out
}

func addCountryRows(by map[string]map[string][]PlaceRow, rangeID string, rows []PlaceRow, head NetworkRow, kind string) {
	for _, r := range tagged(rows, head, kind) {
		if by[r.Key] == nil {
			by[r.Key] = map[string][]PlaceRow{}
		}
		by[r.Key][rangeID] = append(by[r.Key][rangeID], r)
	}
}

func (pg *Pages) addCityRows(rangeID string, rows []PlaceRow, head NetworkRow, kind string) {
	for _, r := range tagged(rows, head, kind) {
		k := listKey{scope: "country/" + model.SV(r.CountryCode), rangeID: rangeID, dimension: "city"}
		pg.lists[k] = append(pg.lists[k], r)
	}
}

// countryAnswers ranks every country's network rows within each category
// (IPs: more is better; latency: lower is better) and builds the index.
func (pg *Pages) countryAnswers(generatedAt string, by map[string]map[string][]PlaceRow) {
	pg.Countries = CountriesAnswer{GeneratedAt: generatedAt, Countries: []CountryIndexRow{}}
	for cc, ranges := range by {
		ans := &CountryAnswer{GeneratedAt: generatedAt, CountryCode: cc, Ranges: map[string][]PlaceRow{}, Cities: map[string]int{}}
		for _, r := range Ranges {
			rows := ranges[r.ID]
			rankPlaces(rows)
			sort.Slice(rows, func(i, j int) bool { return placeLess(rows[i], rows[j]) })
			if rows == nil {
				rows = []PlaceRow{}
			}
			ans.Ranges[r.ID] = rows
			ans.Cities[r.ID] = len(pg.lists[listKey{"country/" + cc, r.ID, "city"}])
			for _, row := range rows {
				if ans.Country == nil {
					ans.Country = row.Country
				}
			}
		}
		pg.Country[cc] = ans
		idx := CountryIndexRow{CountryCode: cc, Country: ans.Country, IpsSum: 0}
		for _, row := range ans.Ranges["30d"] {
			if row.IpsSum.OK() && row.IpsSum > 0 {
				idx.Networks++
				idx.IpsSum += row.IpsSum
			}
		}
		pg.Countries.Countries = append(pg.Countries.Countries, idx)
	}
	for k, rows := range pg.lists {
		if strings.HasPrefix(k.scope, "country/") {
			sort.Slice(rows, func(i, j int) bool { return placeLess(rows[i], rows[j]) })
		}
	}
	sort.Slice(pg.Countries.Countries, func(i, j int) bool {
		a, b := pg.Countries.Countries[i], pg.Countries.Countries[j]
		if a.IpsSum != b.IpsSum {
			return a.IpsSum > b.IpsSum
		}
		return a.CountryCode < b.CountryCode
	})
}

func rankPlaces(rows []PlaceRow) {
	byKind := map[string][2][]Num{}
	for _, r := range rows {
		peers := byKind[r.Kind]
		if r.IpsSum.OK() && r.IpsSum > 0 {
			peers[0] = append(peers[0], r.IpsSum)
		}
		if r.LatencyP50.OK() {
			peers[1] = append(peers[1], r.LatencyP50)
		}
		byKind[r.Kind] = peers
	}
	for i := range rows {
		ips := rows[i].IpsSum
		if !ips.OK() || ips <= 0 {
			ips = nan
		}
		a, b := rankOf(ips, byKind[rows[i].Kind][0], true), rankOf(rows[i].LatencyP50, byKind[rows[i].Kind][1], false)
		rows[i].IpsSumRank, rows[i].LatencyRank = &a, &b
	}
}

// PlacePage is one page of a long place list.
type PlacePage struct {
	GeneratedAt string     `json:"generatedAt"`
	Scope       string     `json:"scope"`
	Range       string     `json:"range"`
	Dimension   string     `json:"dimension"`
	Query       string     `json:"query"`
	Page        int        `json:"page"`
	PageSize    int        `json:"pageSize"`
	Total       int        `json:"total"`
	Pages       int        `json:"pages"`
	Rows        []PlaceRow `json:"rows"`
}

// List is a page of a precomputed list; ok is false for an unknown list.
// The query keeps rows whose code, names or ASN contain every token
// (case-insensitive); pages count from 1.
func (pg *Pages) List(generatedAt, scope, rangeID, dimension, query string, page int) (*PlacePage, bool) {
	if !pg.known(scope, dimension) {
		return nil, false
	}
	rows := pg.lists[listKey{scope, rangeID, dimension}]
	tokens := Tokens(query)
	if len(tokens) > 0 {
		kept := []PlaceRow{}
		for _, r := range rows {
			if matchesAll(r, tokens) {
				kept = append(kept, r)
			}
		}
		rows = kept
	}
	out := &PlacePage{GeneratedAt: generatedAt, Scope: scope, Range: rangeID, Dimension: dimension, Query: strings.Join(tokens, " "),
		Page: page, PageSize: PageSize, Total: len(rows), Pages: (len(rows) + PageSize - 1) / PageSize, Rows: []PlaceRow{}}
	if start := (page - 1) * PageSize; start < len(rows) {
		out.Rows = rows[start:min(len(rows), start+PageSize)]
	}
	return out, true
}

func (pg *Pages) known(scope, dimension string) bool {
	if cc, ok := strings.CutPrefix(scope, "country/"); ok {
		return dimension == "city" && pg.Country[cc] != nil
	}
	return pg.Network[scope] != nil && validDimension(dimension)
}

func validDimension(d string) bool {
	for _, x := range placeDimensions {
		if x == d {
			return true
		}
	}
	return false
}

func matchesAll(r PlaceRow, tokens []string) bool {
	text := strings.ToLower(strings.Join([]string{r.Key, model.SV(r.Country), model.SV(r.Region), model.SV(r.City),
		model.SV(r.AsnName), r.NetworkName, r.Network}, " "))
	if r.Asn != nil {
		text += " as" + strconv.FormatInt(*r.Asn, 10)
	}
	for _, t := range tokens {
		if !strings.Contains(text, t) {
			return false
		}
	}
	return true
}
