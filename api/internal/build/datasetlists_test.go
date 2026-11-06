package build_test

import (
	"bytes"
	"encoding/json"
	"math"
	"sort"
	"strconv"
	"strings"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/build"
	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/Zeus-Development/proxy-index/api/internal/server"
	"github.com/Zeus-Development/proxy-index/api/internal/testscale"
)

// v4Shape is a v4 build of three networks, each one summary over two
// requested countries, with place lists and Coverage lists of two pages and
// more.
var v4Shape = testscale.NetworkShape{Shape: testscale.Shape{Networks: 3, Countries: 1, Exits: 6, Tail: 60, Unmeasured: 1, Events: 5,
	CityAsn: 120}, Requested: 2}

// v4File is the dataset file of a v4Shape build, and the build.
func v4File(t *testing.T) ([]byte, *build.Result) {
	t.Helper()
	in, err := testscale.NetworkInputs(v4Shape)
	if err != nil {
		t.Fatal(err)
	}
	res, err := build.Build(in)
	if err != nil {
		t.Fatal(err)
	}
	var file bytes.Buffer
	if err := build.WriteDataset(&file, res.Dataset); err != nil {
		t.Fatal(err)
	}
	return file.Bytes(), res
}

// The reader below regenerates the list answers from the file alone, by the
// rules FIELDS.md documents for `lists`; it shares no code with the build.

// fileRow is one place row of the file's lists.
type fileRow struct {
	cc, country, regionCode, region, city, asnName model.Str
	asn, ips, requests, successes, p50             model.Num
}

// fileNetwork is one network kind of the file's lists, in file order.
type fileNetwork struct {
	slug, name, kind string
	rows             map[string]map[string][]fileRow // range -> dimension -> rows
	coverage         map[string][]build.CoverageRow  // range -> rows
}

// readLists is the file's generatedAt and its lists in file order.
func readLists(t *testing.T, file []byte) (string, []fileNetwork) {
	t.Helper()
	var doc struct {
		GeneratedAt string `json:"generatedAt"`
		Providers   []struct {
			Slug string `json:"slug"`
			Name string `json:"name"`
		} `json:"providers"`
	}
	if err := json.Unmarshal(file, &doc); err != nil {
		t.Fatal(err)
	}
	names := map[string]string{}
	for _, p := range doc.Providers {
		names[p.Slug] = p.Name
	}
	var out []fileNetwork
	for _, top := range pairs(t, file) {
		if top.key != "lists" {
			continue
		}
		for _, slug := range pairs(t, top.raw) {
			for _, kind := range pairs(t, slug.raw) {
				out = append(out, readKind(t, slug.key, names[slug.key], kind))
			}
		}
	}
	return doc.GeneratedAt, out
}

func readKind(t *testing.T, slug, name string, kind pair) fileNetwork {
	n := fileNetwork{slug: slug, name: name, kind: kind.key, rows: map[string]map[string][]fileRow{}, coverage: map[string][]build.CoverageRow{}}
	for _, rng := range pairs(t, kind.raw) {
		n.rows[rng.key] = map[string][]fileRow{}
		for _, dim := range pairs(t, rng.raw) {
			var rows [][]any
			if err := json.Unmarshal(dim.raw, &rows); err != nil {
				t.Fatal(err)
			}
			for _, r := range rows {
				if dim.key == "city_asn" {
					n.coverage[rng.key] = append(n.coverage[rng.key], coverageOf(r))
				} else {
					n.rows[rng.key][dim.key] = append(n.rows[rng.key][dim.key], placeOf(dim.key, r))
				}
			}
		}
	}
	return n
}

func str(v any) model.Str {
	if s, ok := v.(string); ok {
		return &s
	}
	return nil
}

func num(v any) model.Num {
	if f, ok := v.(float64); ok {
		return model.Num(f)
	}
	return model.Num(math.NaN())
}

// placeOf reads a place row by its dimension's documented field order.
func placeOf(dim string, r []any) fileRow {
	var f fileRow
	switch dim {
	case "asn":
		f.asn, f.asnName, r = num(r[0]), str(r[1]), r[2:]
	case "country":
		f.cc, f.country, r = str(r[0]), str(r[1]), r[2:]
	case "region":
		f.cc, f.country, f.regionCode, f.region, r = str(r[0]), str(r[1]), str(r[2]), str(r[3]), r[4:]
	case "city":
		f.cc, f.country, f.regionCode, f.region, f.city, r = str(r[0]), str(r[1]), str(r[2]), str(r[3]), str(r[4]), r[5:]
	}
	f.ips, f.requests, f.successes, f.p50 = num(r[0]), num(r[1]), num(r[2]), num(r[3])
	if dim != "asn" {
		f.asn = model.Num(math.NaN())
	}
	return f
}

func coverageOf(r []any) build.CoverageRow {
	return build.CoverageRow{CoverageRow: model.CoverageRow{CountryCode: str(r[0]), Country: str(r[1]), Region: str(r[2]), City: str(r[3]),
		Asn: num(r[4]), AsnName: str(r[5]), UsageType: str(r[6]), Ips: num(r[7]), LatencyP50: num(r[8])}, Rank: num(r[9]), Peers: int(num(r[10]))}
}

// placeKey is a row's place key: the upper-case country code, then region
// code, region and city by dimension, or the ASN number.
func placeKey(dim string, r fileRow) (string, bool) {
	cc := strings.ToUpper(strings.TrimSpace(model.SV(r.cc)))
	parts := map[string][]string{"country": {cc}, "region": {cc, model.SV(r.regionCode), model.SV(r.region)},
		"city": {cc, model.SV(r.regionCode), model.SV(r.region), model.SV(r.city)}}[dim]
	if dim == "asn" {
		return strconv.FormatInt(int64(r.asn), 10), r.asn.OK()
	}
	return strings.Join(parts, "|"), cc != ""
}

func plus(a, b model.Num) model.Num {
	switch {
	case !b.OK():
		return a
	case !a.OK():
		return b
	}
	return a + b
}

// merged is rows merged by place key: IPs, requests and successes add up,
// the latency is the first row's with the most successes (a row counts only
// with a latency and successes), every other field the first row's.
func merged(dim string, rows []fileRow, keep func(fileRow) bool) []build.PlaceRow {
	byKey, most := map[string]*build.PlaceRow{}, map[string]model.Num{}
	var keys []string
	for _, r := range rows {
		key, ok := placeKey(dim, r)
		if !ok || keep != nil && !keep(r) {
			continue
		}
		counts := r.p50.OK() && r.p50 >= 0 && r.successes.OK() && r.successes > 0
		cur := byKey[key]
		if cur == nil {
			cur = &build.PlaceRow{Key: key, CountryCode: r.cc, Country: r.country, RegionCode: r.regionCode, Region: r.region, City: r.city,
				AsnName: r.asnName, IpsSum: r.ips, Requests: r.requests, Successes: r.successes, LatencyP50: model.Num(math.NaN())}
			if dim == "asn" {
				n := int64(r.asn)
				cur.Asn = &n
			} else {
				cc, _, _ := strings.Cut(key, "|")
				cur.CountryCode = &cc
			}
			most[key] = model.Num(math.NaN())
			if counts {
				cur.LatencyP50, most[key] = r.p50, r.successes
			}
			byKey[key] = cur
			keys = append(keys, key)
			continue
		}
		cur.IpsSum, cur.Requests = plus(cur.IpsSum, r.ips), plus(cur.Requests, r.requests)
		if counts && (!most[key].OK() || r.successes > most[key]) {
			cur.LatencyP50, most[key] = r.p50, r.successes
		}
		cur.Successes = plus(cur.Successes, r.successes)
	}
	out := []build.PlaceRow{}
	for _, k := range keys {
		r := *byKey[k]
		r.SuccessRate = model.Num(math.NaN())
		if r.Successes.OK() && r.Requests.OK() && r.Requests > 0 {
			r.SuccessRate = r.Successes / r.Requests
		}
		out = append(out, r)
	}
	return out
}

// ordered sorts rows most IPs first (none counts as -1), then by key, then
// network and kind.
func ordered(rows []build.PlaceRow) []build.PlaceRow {
	ips := func(r build.PlaceRow) model.Num {
		if !r.IpsSum.OK() {
			return -1
		}
		return r.IpsSum
	}
	sort.SliceStable(rows, func(i, j int) bool {
		a, b := rows[i], rows[j]
		if ips(a) != ips(b) {
			return ips(a) > ips(b)
		}
		if a.Key != b.Key {
			return a.Key < b.Key
		}
		return a.Network+a.Kind < b.Network+b.Kind
	})
	return rows
}

// networkRows is a network's merged rows of a range and dimension.
func (n fileNetwork) networkRows(rangeID, dim string, keep func(fileRow) bool) []build.PlaceRow {
	return ordered(merged(dim, n.rows[rangeID][dim], keep))
}

// acrossNetworks is every network's rows merged apart and tagged with it, as
// a country's lists and the place pages show them.
func acrossNetworks(lists []fileNetwork, rangeID, dim string, keep func(fileRow) bool) []build.PlaceRow {
	out := []build.PlaceRow{}
	for _, n := range lists {
		for _, r := range n.networkRows(rangeID, dim, keep) {
			r.Network, r.NetworkName, r.Kind = n.slug, n.name, n.kind
			out = append(out, r)
		}
	}
	return ordered(out)
}

// matching keeps the rows whose key, names, network or "as<n>" hold every
// word of q.
func matching(rows []build.PlaceRow, q string) []build.PlaceRow {
	out := []build.PlaceRow{}
	for _, r := range rows {
		text := strings.ToLower(strings.Join([]string{r.Key, model.SV(r.Country), model.SV(r.Region), model.SV(r.City), model.SV(r.AsnName),
			r.NetworkName, r.Network}, " "))
		if r.Asn != nil {
			text += " as" + strconv.FormatInt(*r.Asn, 10)
		}
		if holdsAll(q, text) {
			out = append(out, r)
		}
	}
	return out
}

func holdsAll(q string, texts ...string) bool {
	for _, w := range strings.Fields(strings.ToLower(q)) {
		found := false
		for _, t := range texts {
			found = found || strings.Contains(t, w)
		}
		if !found {
			return false
		}
	}
	return true
}

// page is a list's page as a PlacePage.
func page(at, scope, rangeID, dim, q string, n int, rows []build.PlaceRow) *build.PlacePage {
	rows = matching(rows, q)
	out := &build.PlacePage{GeneratedAt: at, Scope: scope, Range: rangeID, Dimension: dim, Query: strings.Join(strings.Fields(strings.ToLower(q)), " "),
		Page: n, PageSize: build.PageSize, Total: len(rows), Pages: (len(rows) + build.PageSize - 1) / build.PageSize, Rows: []build.PlaceRow{}}
	if start := (n - 1) * build.PageSize; start < len(rows) {
		out.Rows = rows[start:min(len(rows), start+build.PageSize)]
	}
	return out
}

// sameBody fails when the regenerated answer is not the served one.
func sameBody(t *testing.T, what string, served any, ok bool, regenerated any) {
	t.Helper()
	want, err := server.Marshal(served)
	if err != nil || !ok {
		t.Fatalf("%s: not served (%v)", what, err)
	}
	got, err := server.Marshal(regenerated)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(got, want) {
		t.Errorf("%s: the file does not regenerate the answer:\n got %.300s\nwant %.300s", what, got, want)
	}
}

// listQueries are the recorded queries of each dimension's lists.
var listQueries = map[string]string{"country": "AA", "region": "region 1", "city": "city 1", "asn": "as6451"}

// pagesOf is the pages a list check asks without a query: the first and the
// last.
func pagesOf(total int) []int { return []int{1, max(1, (total+build.PageSize-1)/build.PageSize)} }
