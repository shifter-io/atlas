package build

import (
	"context"
	"slices"
	"sort"
	"strconv"
	"strings"

	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/snap"
)

// listKey names one precomputed place list: scope is "slug/kind" for a
// network's list or "country/CC" for a country's cities across networks.
type listKey struct{ scope, rangeID, dimension string }

type placeList struct{ rangeID, dimension string }

// networkAnswer sums a network's product groups of one category: its 30-day
// days, and per range its totals and every place list in order (the top
// countries go into the answer, every list into the pages). The lists are
// merged from the groups' network lists, which it also gives per range
// (srcs); the groups are network summaries.
func networkAnswer(generatedAt string, head NetworkRow, kind string, groups []*snap.Product,
	nr *netRead) (ans *NetworkAnswer, lists map[placeList][]PlaceRow, srcs map[string][]placeSource) {
	ans = &NetworkAnswer{GeneratedAt: generatedAt, Network: head, Kind: kind, ProductGroups: productGroups(groups),
		RequestedCountries: requestedCountries(groups), RequestedCountryStatus: requestedCountryStatus(groups),
		Days: days(groups), Ranges: map[string]*RangeTotals{}, Stats: map[string]*RangeStats{},
		Reviews: []model.UserReview{}, CommunityRuns: []model.CommunityRun{}, Changelog: []ChangeEntry{}, Tape: []model.TapeEvent{},
		groups: groups}
	lists, srcs = map[placeList][]PlaceRow{}, map[string][]placeSource{}
	for _, r := range Ranges {
		t, rows, s := rangeAnswer(head, kind, groups, nr, r.ID)
		for d, l := range rows {
			lists[placeList{r.ID, d}] = l
		}
		ans.Ranges[r.ID], srcs[r.ID] = t, s
	}
	return ans, lists, srcs
}

// rangeAnswer is a network's totals of one range, its place lists by
// dimension and the network lists they are merged from.
func rangeAnswer(head NetworkRow, kind string, groups []*snap.Product, nr *netRead,
	rangeID string) (*RangeTotals, map[string][]PlaceRow, []placeSource) {
	t := &RangeTotals{Requests: 0, Successes: 0, IpsSum: nan, UniqueIps: nan, DatacenterIps: nan, LatencyP50: nan, LatencyP90: nan, LatencyP99: nan,
		Lists: map[string]int{}, Targets: []TargetTraffic{}}
	var srcs []placeSource
	for _, p := range groups {
		w := p.Window(rangeID)
		t.Requests, t.Successes = addNum(t.Requests, w.Metrics.Completed), addNum(t.Successes, w.Metrics.Successes)
		t.IpsSum = addNum(t.IpsSum, ipsOf(w))
		if l := netListOf(nr.row, p, w); l != nil {
			srcs = append(srcs, placeSource{list: l, scale: breakdownScale(w), network: head.Slug, name: model.SV(head.Name), kind: kind})
		}
	}
	if len(groups) == 0 {
		t.Requests, t.Successes = nan, nan
	}
	t.SuccessRate = ratio(t.Successes, t.Requests)
	if len(groups) == 1 {
		// One group: its own distinct IPs and latency are the network's.
		w := groups[0].Window(rangeID)
		t.UniqueIps, t.LatencyP50, t.LatencyP90, t.LatencyP99 = ipsOf(w), w.Metrics.LatencyMs.P50, w.Metrics.LatencyMs.P90, w.Metrics.LatencyMs.P99
		t.DatacenterIps = datacenterIps(w)
	}
	rows := map[string][]PlaceRow{}
	for _, d := range placeDimensions {
		rows[d] = nr.merge(srcs, d)
		t.Lists[d] = len(rows[d])
	}
	countries := rows["country"]
	t.ExitCountries = len(countries)
	t.Countries = countries[:min(len(countries), TopCountries)]
	return t, rows, srcs
}

// productGroups is how many product groups the groups stand for: a network
// summary for its criteria.groups (one when it names none).
func productGroups(groups []*snap.Product) int {
	n := 0
	for _, p := range groups {
		n++
		if g := p.Criteria.Groups; g.OK() && g > 1 {
			n += int(g) - 1
		}
	}
	return n
}

// requestedCountries is the groups' requested countries, upper case and
// sorted: each network summary's own list.
func requestedCountries(groups []*snap.Product) []string {
	seen := map[string]bool{}
	out := []string{}
	for _, p := range groups {
		for _, cc := range p.Criteria.RequestedCountries {
			if cc = strings.ToUpper(cc); cc != "" && !seen[cc] {
				seen[cc] = true
				out = append(out, cc)
			}
		}
	}
	sort.Strings(out)
	return out
}

// days sums the groups' 30-day series (one row per UTC day) by day. A day's
// distinct IPs are known for a single group only (allAnswer sets them from
// the snapshot's every-group fleet).
func days(groups []*snap.Product) []DayRow {
	byDay := map[string]*DayRow{}
	for _, p := range groups {
		w := p.Window("30d")
		for _, s := range w.Series {
			day := dayOf(s.At)
			d := byDay[day]
			if d == nil {
				d = &DayRow{Day: day, Requests: 0, Successes: 0, IpsSum: nan, UniqueIps: nan}
				byDay[day] = d
			}
			d.Requests, d.Successes, d.IpsSum = addNum(d.Requests, s.Completed), addNum(d.Successes, s.Successes), addNum(d.IpsSum, dayIps(w, s))
			if len(groups) == 1 {
				d.UniqueIps = dayIps(w, s)
			}
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
		r.Network, r.NetworkName, r.Kind = head.Slug, model.SV(head.Name), kind
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
			ans.Cities[r.ID] = 0
			if l := pg.lists[listKey{"country/" + cc, r.ID, "city"}]; l != nil {
				ans.Cities[r.ID] = l.total
			}
		}
		pg.Country[cc] = ans
		idx := CountryIndexRow{CountryCode: cc, IpsSum: 0}
		for _, row := range ans.Ranges["30d"] {
			if row.IpsSum.OK() && row.IpsSum > 0 {
				idx.Networks++
				idx.IpsSum += row.IpsSum
			}
		}
		pg.Countries.Countries = append(pg.Countries.Countries, idx)
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
		rows[i].IpsSumRank, rows[i].LatencyRank, rows[i].IpsRank = &a, &b, nil
	}
}

// PlacePage is one page of a long place list; its na lists its rows' nulls,
// each not_measured_per_place: a row's requests and latency are its own only
// where a test records them per place (pulses), not for daily tests, which
// record them per requested country.
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
	NAList
}

// ListFilter is the rows a list page keeps: those whose codes, names or ASN
// contain every token of Query (case-insensitive) and, when Country (an
// upper-case code) is set, those in that country (an ASN row has none).
type ListFilter struct{ Query, Country string }

// List is a page of a list; ok is false for an unknown list. Pages count
// from 1. The first page without a filter is the one the build computed;
// another page merges the list's rows (placeIndex), read from the database
// unless cached.
func (pg *Pages) List(ctx context.Context, generatedAt, scope, rangeID, dimension string, f ListFilter, page int) (*PlacePage, bool, error) {
	if !pg.known(scope, dimension) {
		return nil, false, nil
	}
	tokens := Tokens(f.Query)
	out := &PlacePage{GeneratedAt: generatedAt, Scope: scope, Range: rangeID, Dimension: dimension, Query: strings.Join(tokens, " "),
		Page: page, PageSize: PageSize, Rows: []PlaceRow{}}
	l := pg.lists[listKey{scope, rangeID, dimension}]
	if l == nil {
		return out, true, nil
	}
	rows := l.first
	if page > 1 || len(tokens) > 0 || f.Country != "" {
		var err error
		if rows, err = l.rows(ctx, tokens, f.Country); err != nil {
			return nil, false, err
		}
		if start := (page - 1) * PageSize; start < len(rows) {
			out.Rows = rows[start:min(len(rows), start+PageSize)]
		}
		out.Total = len(rows)
	} else {
		out.Rows, out.Total = append(out.Rows, rows...), l.total
	}
	out.Pages = (out.Total + PageSize - 1) / PageSize
	pg.scoreRows(scope, rangeID, dimension, out.Rows)
	out.ListNA()
	return out, true, nil
}

// ListNA lists the page's null row figures (rows[].<figure>, each once),
// every one not_measured_per_place but a successScore's, which has its
// status: what naOf would list, without its JSON round trip per page.
func (p *PlacePage) ListNA() {
	paths := []string{}
	for _, r := range p.Rows {
		for _, f := range [...]struct {
			path string
			null bool
		}{{"rows[].ipsSum", !r.IpsSum.OK()}, {"rows[].requests", !r.Requests.OK()}, {"rows[].successes", !r.Successes.OK()},
			{"rows[].latencyP50", !r.LatencyP50.OK()},
			{"rows[].ipsSumRank.rank", r.IpsSumRank != nil && !r.IpsSumRank.Rank.OK()},
			{"rows[].latencyRank.rank", r.LatencyRank != nil && !r.LatencyRank.Rank.OK()}} {
			if f.null && !slices.Contains(paths, f.path) {
				paths = append(paths, f.path)
			}
		}
	}
	p.NAList.set(withScoreNA(paths, p.Rows))
}

func (pg *Pages) known(scope, dimension string) bool {
	if cc, ok := strings.CutPrefix(scope, "country/"); ok {
		return (dimension == "city" || dimension == "region") && pg.Country[cc] != nil
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
	text := strings.ToLower(strings.Join([]string{r.Key, model.SV(r.Region), model.SV(r.City),
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
