package server

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"sort"
	"strings"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/build"
)

// The English guard (ENGLISH-GUARD-1, #976): the API serves values, never
// words for display, so a website in any language writes its own. Every
// string an answer, a sample, a list page, the dataset file, an error answer
// or /healthz serves must be an id, a code, an ISO time, a URL, a hash or a
// value of a name path (namePaths). The English still served is listed, by
// area, in testdata/english-pending/<area>.txt with the task that removes it;
// a path it does not list fails, and so does a listed path that is clean (its
// task deletes its lines). #962's prose scan guards removed field names; this
// one guards values.

// namePaths are the endings of normalized paths whose values are names as
// their owner writes them: a network's, the provider's plan, tier, pool and
// coupon names, a place's region, city and network-operator (ASN) names and
// its key, what a user wrote in a review or a run, and /healthz's operator
// diagnostics. notNames are paths these endings would match that hold our
// own English instead. A plan's name and its catalog pool are the
// provider's when they are names, ours when they are sentences or
// written-out numbers ("Pay as you go", "10 GB"): catalogNames fail on those.
var namePaths = []string{
	".name", ".providerName", ".networkName", ".catalog.tier", ".catalog.pool", ".catalog.code",
	".region", ".city", ".asnName", ".key", "reviews[].author", "reviews[].role", "reviews[].title", "reviews[].body",
	"reviews[].providerReply", ".submittedBy", "/healthz.lastError", "/healthz.failedError",
}

var catalogNames = []string{"plans[].name", ".catalog.pool"}

var notNames = []string{"/all.network.name", ".products[].name", "/v1/coverage.targets[].name",
	"/v1/coverage.scopes[].name", "/v1/coverage.profiles[].name", "runs[].region", "communityRuns[].region"}

// nameBodies are bodies every value of which is a name or reference text:
// the codes catalog (#975) holds the one English sentence of each code.
var nameBodies = []string{"/v1/codes"}

var (
	relativeTime = regexp.MustCompile(`(?i)^(now|.*\bago)$`)
	isoTime      = regexp.MustCompile(`^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}(:?\d{2})?)?)?$`)
	urlValue     = regexp.MustCompile(`^([a-z][a-z0-9+.-]*://|/)\S*$`)
	// codeSeparators part an id, a code or an na path; each part is
	// lowercase (or camelCase, a field name), or upper case (US, AS12345).
	codeSeparators = regexp.MustCompile(`[_\-:./+\[\]]`)
	codePart       = regexp.MustCompile(`^([a-z][a-zA-Z0-9]*|[0-9]+[a-z0-9]*|[A-Z0-9]+)?$`)
	writtenNumber  = regexp.MustCompile(`(?i)\d[\d,]*(\.\d+)?\s*(%|[kmgt]b\b|days?\b|hours?\b|months?\b)|\$\s?\d|\d,\d{3}`)
	sentenceMark   = regexp.MustCompile(`[.!?;:,)](\s|$)`)
	countryKey     = regexp.MustCompile(`^[A-Z]{2}$`)
)

// englishKind is why s is English: a relative time, a written-out number, a
// sentence (4 words or more, or sentence punctuation) or a display word; ""
// when s is an id, a code, an ISO time, a URL or a hash.
func englishKind(s string) string {
	switch {
	case relativeTime.MatchString(strings.TrimSpace(s)):
		return "relative time"
	case s == "" || isoTime.MatchString(s) || isCode(s):
		return ""
	case writtenNumber.MatchString(s):
		return "written-out number"
	case !strings.ContainsAny(s, " \t\n") && urlValue.MatchString(s):
		return ""
	case len(strings.Fields(s)) >= 4 || sentenceMark.MatchString(s):
		return "sentence"
	}
	return "display word"
}

// isCode is whether s is an id, a slug, a code, a hash or an na path: no
// spaces, and every part between separators lowercase or upper case.
func isCode(s string) bool {
	if strings.ContainsAny(s, " \t\n") {
		return false
	}
	for _, part := range codeSeparators.Split(s, -1) {
		if !codePart.MatchString(part) {
			return false
		}
	}
	return true
}

// nameAllows is whether p (a normalized path) holds names, so that a value
// of kind (englishKind) is no English there.
func nameAllows(p, kind string) bool {
	if !isName(p) {
		return false
	}
	if kind != "sentence" && kind != "written-out number" {
		return true
	}
	for _, n := range catalogNames {
		if strings.HasSuffix(p, n) {
			return false
		}
	}
	return true
}

// isName is whether p (a normalized path) holds names.
func isName(p string) bool {
	for _, b := range nameBodies {
		if strings.HasPrefix(p, b+".") {
			return true
		}
	}
	for _, n := range notNames {
		if strings.HasSuffix(p, n) {
			return false
		}
	}
	for _, n := range namePaths {
		if strings.HasSuffix(p, n) {
			return true
		}
	}
	return false
}

// englishHit is an English path's kind and one example value.
type englishHit struct{ kind, example string }

// englishOf adds, in v (a body's generic JSON), every English string's
// normalized path to hits; an naReasons key is an na path, checked as one.
func englishOf(v any, path, parent string, hits map[string]englishHit) {
	switch x := v.(type) {
	case map[string]any:
		for k, child := range x {
			p := path + "." + englishLabel(k, parent)
			if parent == "naReasons" {
				addEnglish(hits, normalize(p)+" (key)", k, englishKind(k))
			}
			if cols := columnsOf(x, k); cols != nil {
				rowsOf(child, p, cols, hits)
				continue
			}
			englishOf(child, p, k, hits)
		}
	case []any:
		for _, child := range x {
			englishOf(child, path+"[]", parent+"[]", hits)
		}
	case string:
		p, kind := normalize(path), englishKind(x)
		if !nameAllows(p, kind) {
			addEnglish(hits, p, x, kind)
		}
	}
}

// columnsOf is the column names of x's key k when k is "rows" and x has a
// "columns" list of strings (the positions of each row), else nil.
func columnsOf(x map[string]any, k string) []string {
	list, ok := x["columns"].([]any)
	if k != "rows" || !ok {
		return nil
	}
	cols := make([]string, len(list))
	for i, c := range list {
		if cols[i], ok = c.(string); !ok {
			return nil
		}
	}
	return cols
}

// rowsOf walks rows whose positions are named by cols: each cell's path
// ends in its column's name, so that a name column is a name path.
func rowsOf(rows any, path string, cols []string, hits map[string]englishHit) {
	list, _ := rows.([]any)
	for _, row := range list {
		cells, _ := row.([]any)
		for i, cell := range cells {
			col := "[]"
			if i < len(cols) {
				col = "." + cols[i]
			}
			englishOf(cell, path+"[]"+col, strings.TrimPrefix(col, "."), hits)
		}
	}
}

func addEnglish(hits map[string]englishHit, p, s, kind string) {
	if _, ok := hits[p]; !ok && kind != "" {
		hits[p] = englishHit{kind, s}
	}
}

// variants are the ranges, kinds and engines: as a map key they (and
// country codes) are <key>,
// and englishGroup drops them from a body's path, so that one English path
// is listed once for all of them.
var variants = map[string]bool{"24h": true, "7d": true, "30d": true, "residential": true, "datacenter": true, "mobile": true,
	"isp": true, "http": true, "chrome": true, "camoufox": true}

func englishLabel(k, parent string) string {
	switch {
	case parent == "pages":
		return englishGroup(k)
	case variants[k] || countryKey.MatchString(k):
		return "<key>"
	case strings.Contains(k, "."): // a field path as a key (the N/A report, naReasons)
		parts := strings.Split(k, ".")
		for i, p := range parts {
			parts[i] = englishLabel(p, "")
		}
		return strings.Join(parts, ".")
	}
	return label(k, parent)
}

// englishGroup names a body by its public route, as a served API's paths
// are: the stored path without its variants (and the all-networks answer's
// second "all"), a chart as /v1/networks/{slug}/chart, a place page without
// its key, the dataset file's route as "dataset file", then bodyGroup.
func englishGroup(name string) string {
	name, _, _ = strings.Cut(name, "?")
	if name == "/v1/dataset" {
		return "dataset file"
	}
	parts := strings.Split(name, "/")
	kept := append([]string{}, parts[:min(3, len(parts))]...)
	for i, p := range parts[min(3, len(parts)):] {
		if !variants[p] && (p != "all" || i == 0) {
			kept = append(kept, p)
		}
	}
	switch {
	case len(kept) == 4 && kept[2] == "charts":
		kept = []string{"", "v1", "networks", kept[3], "chart"}
	case len(kept) > 4 && kept[2] == "places":
		kept = kept[:4]
	}
	return bodyGroup(strings.Join(kept, "/"))
}

// scanEnglish is every English path of bodies, with its kind and example.
func scanEnglish(bodies map[string][]byte) map[string]englishHit {
	hits := map[string]englishHit{}
	for name, b := range bodies {
		var doc any
		if json.Unmarshal(b, &doc) != nil {
			continue // not a JSON body
		}
		englishOf(doc, englishGroup(name), "", hits)
	}
	return hits
}

// errorPaths answer errors: a 404, a bad range, an unknown parameter and a
// limit out of range.
var errorPaths = []string{"/v1/nope", "/v1/rankings?range=99d", "/v1/home?foo=1", "/v1/events?limit=9999"}

// englishBodies is scanBodies, every registered answer's samples, the
// events and search answers, the error answers (404, a bad range, an unknown
// parameter, a limit out of range) and /healthz, serving and before payload
// v4. A query after ? only tells bodies of one path apart.
func englishBodies(t *testing.T) map[string][]byte {
	t.Helper()
	bodies, srv := scanServer(t)
	v := srv.current.Load()
	srv.state.unserved, srv.state.unservedMore = srv.unservedPages(v)
	h := srv.Handler()
	paths := []string{"/v1/events", "/v1/events?limit=5", "/v1/search?q=net", "/v1/search?q=amazon", "/healthz",
		"/v1/networks/net00/coverage", "/v1/networks/net00/chart", "/v1/networks/all/chart"}
	paths = append(paths, errorPaths...)
	for _, a := range build.Answers() {
		paths = append(paths, a.Samples...)
	}
	for _, p := range paths {
		bodies[p] = get(h, p).Body.Bytes()
	}
	for _, p := range placePaths(t, h, bodies["/v1/countries"]) {
		bodies[p] = get(h, p).Body.Bytes()
	}
	before := New(&fakeSource{}, Options{})
	before.state.reason = BeforeV4Reason
	for _, p := range []string{"/healthz", "/v1/home"} {
		bodies[p+"?before-v4"] = get(before.Handler(), p).Body.Bytes()
	}
	return bodies
}

// placePaths are the first country's cities and regions, and the first city
// and region page.
func placePaths(t *testing.T, h http.Handler, countries []byte) []string {
	t.Helper()
	var doc struct {
		Countries []struct {
			Code string `json:"countryCode"`
		} `json:"countries"`
	}
	if err := json.Unmarshal(countries, &doc); err != nil || len(doc.Countries) == 0 {
		t.Fatalf("/v1/countries: %d countries, %v", len(doc.Countries), err)
	}
	var out []string
	for list, dimension := range map[string]string{"cities": "city", "regions": "region"} {
		p := "/v1/countries/" + doc.Countries[0].Code + "/" + list
		var rows struct {
			Rows []struct {
				Key string `json:"key"`
			} `json:"rows"`
		}
		if err := json.Unmarshal(get(h, p).Body.Bytes(), &rows); err != nil || len(rows.Rows) == 0 {
			t.Fatalf("%s: %d rows, %v", p, len(rows.Rows), err)
		}
		out = append(out, p, "/v1/places/"+dimension+"/"+url.PathEscape(rows.Rows[0].Key)+"?range=30d")
	}
	return out
}

// TestNoEnglishInAnswers walks every body of the scan build (englishBodies)
// and fails on English the pending list does not name, and on a listed path
// that is clean.
func TestNoEnglishInAnswers(t *testing.T) {
	t.Parallel()
	pending := readPending(t)
	hits := scanEnglish(englishBodies(t))
	dumpEnglish(t, hits)
	checkPending(t, hits, pending, true)
}

// dumpEnglish logs every English path with its kind and example when
// ENGLISH_SCAN_DUMP is set, to write the pending list from.
func dumpEnglish(t *testing.T, hits map[string]englishHit) {
	if os.Getenv("ENGLISH_SCAN_DUMP") == "" {
		return
	}
	list := make([]string, 0, len(hits))
	for p, h := range hits {
		list = append(list, fmt.Sprintf("%s\t%s\t%q", p, h.kind, h.example))
	}
	sort.Strings(list)
	t.Logf("%d English paths:\n%s", len(list), strings.Join(list, "\n"))
}

func TestEnglishKind(t *testing.T) {
	t.Parallel()
	for s, want := range map[string]string{
		"oxylabs": "", "amazon_pdp": "", "hard-targets": "", "US": "", "AS12345": "", "A+": "", "24h": "", "loginWall": "",
		"2026-11-13T15:38:08Z": "", "2026-11-13": "", "2026-11-10 00:00:00+00:00": "", "https://example.com/a?b=1": "",
		"/v1/networks/oxylabs": "", "3f2a9c0d": "", "pricing.plans[3].ips": "", "residential_rotating:24h": "",
		"outage:net00:residential:network:CU:2026-11-09T06:32:38+00:00": "", "": "",
		"29.8%": "written-out number", "5,807,186": "written-out number", "$2.00": "written-out number",
		"1 TB": "written-out number", "5 GB": "written-out number", "/30 days": "written-out number",
		"6h ago": "relative time", "2 days ago": "relative time", "now": "relative time", "23h 45m ago": "relative time",
		"The product page loads with its title.": "sentence", "Pay as you go": "sentence", "Vilnius, Lithuania": "sentence",
		"pricing.plans[Pay as you go].ips": "sentence", "Residential": "display word", "Balanced": "display word",
		"not found": "display word", "%": "display word", "96.5 · #1": "display word", "Hard targets": "display word",
	} {
		if got := englishKind(s); got != want {
			t.Errorf("englishKind(%q) = %q, want %q", s, got, want)
		}
	}
}

func TestEnglishPathsAndNames(t *testing.T) {
	t.Parallel()
	doc := `{"network": {"name": "Oxy Labs", "slug": "oxylabs"}, "rows": [{"city": "Ho Chi Minh City", "country": "Viet Nam"}],
		"profile": {"products": [{"name": "Residential"}], "naReasons": {"pricing.plans[1 GB].ips": "catalog", "stats.p50": "not_measured"},
			"pricing": {"plans": [{"name": "Starter", "catalog": {"pool": "Full Geo", "period": "/30 days"}}, {"name": "Pay as you go"}]}},
		"reviews": [{"title": "Great value", "verificationLabel": "Invoice verified"}], "labels": [["6h ago"]],
		"places": {"columns": ["countryCode", "asnName", "count"], "rows": [["US", "Comcast Cable Communications, LLC", 3]]},
		"table": {"rows": [["Egypt", 3]]}, "fields": {"latencyRanks.residential.24h.AT.p50": {"reason": "No value for this field"}},
		"sections": {"productMeasurements.mobile.pool": [{"reason": "Not published by the snapshot"}]}}`
	hits := scanEnglish(map[string][]byte{"/v1/networks/a/residential": []byte(doc), "/v1/networks/all/all": []byte(`{"network": {"name": "All networks"}}`),
		"/v1/codes": []byte(`{"sets": [{"codes": [{"en": "The product page loads."}]}]}`), "/healthz": []byte(`{"lastError": "dial tcp: refused"}`)})
	var got []string
	for p := range hits {
		got = append(got, p)
	}
	sort.Strings(got)
	want := []string{"/v1/networks/*.fields.latencyRanks.<key>.<key>.<key>.p50.reason", "/v1/networks/*.labels[][]", "/v1/networks/*.profile.naReasons.pricing.plans[].ips (key)",
		"/v1/networks/*.profile.pricing.plans[].catalog.period", "/v1/networks/*.profile.pricing.plans[].name",
		"/v1/networks/*.profile.products[].name", "/v1/networks/*.reviews[].verificationLabel", "/v1/networks/*.rows[].country",
		"/v1/networks/*.sections.productMeasurements.<key>.pool[].reason",
		"/v1/networks/*.table.rows[][]", "/v1/networks/all.network.name"}
	if strings.Join(got, ",") != strings.Join(want, ",") {
		t.Errorf("found %v, want %v", got, want)
	}
}
