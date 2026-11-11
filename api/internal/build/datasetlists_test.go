package build_test

import (
	"bytes"
	"encoding/json"
	"math"
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

// The reader below regenerates each list's first page from the file alone,
// by the rules FIELDS.md documents for `lists`; it shares no code with the
// build.

// fileRow is one place row of the file's lists.
type fileRow struct {
	cc, country, regionCode, region, city, asnName model.Str
	asn, ips, requests, successes, p50             model.Num
}

// fileList is one list of the file: its counts and its first page.
type fileList struct {
	total, pages int
	rows         []fileRow
	coverage     []build.CoverageRow
}

// fileNetwork is one network kind of the file's lists, in file order.
type fileNetwork struct {
	slug, kind string
	lists      map[string]map[string]fileList // range -> dimension -> list
}

// readLists is the file's generatedAt and its lists in file order.
func readLists(t *testing.T, file []byte) (string, []fileNetwork) {
	t.Helper()
	var doc struct {
		GeneratedAt string `json:"generatedAt"`
	}
	if err := json.Unmarshal(file, &doc); err != nil {
		t.Fatal(err)
	}
	var out []fileNetwork
	for _, top := range pairs(t, file) {
		if top.key != "lists" {
			continue
		}
		for _, slug := range pairs(t, top.raw) {
			for _, kind := range pairs(t, slug.raw) {
				out = append(out, readKind(t, slug.key, kind))
			}
		}
	}
	return doc.GeneratedAt, out
}

func readKind(t *testing.T, slug string, kind pair) fileNetwork {
	n := fileNetwork{slug: slug, kind: kind.key, lists: map[string]map[string]fileList{}}
	for _, rng := range pairs(t, kind.raw) {
		n.lists[rng.key] = map[string]fileList{}
		for _, dim := range pairs(t, rng.raw) {
			var l struct {
				Total int     `json:"total"`
				Pages int     `json:"pages"`
				Rows  [][]any `json:"rows"`
			}
			if err := json.Unmarshal(dim.raw, &l); err != nil {
				t.Fatal(err)
			}
			f := fileList{total: l.Total, pages: l.Pages}
			for _, r := range l.Rows {
				if dim.key == "city_asn" {
					f.coverage = append(f.coverage, coverageOf(r))
				} else {
					f.rows = append(f.rows, placeOf(dim.key, r))
				}
			}
			n.lists[rng.key][dim.key] = f
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

// placeRow is a file row as the place row a page shows: with its key, its
// success rate (successes / requests when both are known and requests are
// above 0) and, in the ASN dimension, its number.
func placeRow(dim string, r fileRow) build.PlaceRow {
	key, _ := placeKey(dim, r)
	out := build.PlaceRow{Key: key, CountryCode: r.cc, Country: r.country, RegionCode: r.regionCode, Region: r.region, City: r.city,
		AsnName: r.asnName, IpsSum: r.ips, Requests: r.requests, Successes: r.successes, SuccessRate: model.Num(math.NaN()), LatencyP50: r.p50}
	if dim == "asn" {
		n := int64(r.asn)
		out.Asn = &n
	}
	if r.successes.OK() && r.requests.OK() && r.requests > 0 {
		out.SuccessRate = r.successes / r.requests
	}
	return out
}

// firstPage is the first page without a query of a network kind's list, as
// the file gives it.
func (l fileList) firstPage(at, scope, rangeID, dim string) *build.PlacePage {
	out := &build.PlacePage{GeneratedAt: at, Scope: scope, Range: rangeID, Dimension: dim, Page: 1, PageSize: build.PageSize,
		Total: l.total, Pages: l.pages, Rows: []build.PlaceRow{}}
	for _, r := range l.rows {
		out.Rows = append(out.Rows, placeRow(dim, r))
	}
	out.ListNA() // derived from the rows alone
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
