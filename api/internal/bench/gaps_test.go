package bench

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/Zeus-Development/atlas/api/internal/server"
	"github.com/Zeus-Development/atlas/api/internal/store"
	"github.com/Zeus-Development/atlas/api/internal/testscale"
)

// gapServer answers the bench's shape requests, a list answer per range
// (a priced row and a row with an unexplained null) and a network answer
// whose null is listed in its profile's na.
func gapServer(t *testing.T) string {
	t.Helper()
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/v1/version":
			fmt.Fprint(w, `{"revision":"abc"}`)
		case "/v1/networks":
			fmt.Fprint(w, `{"networks":[{"slug":"a","kind":"residential","kinds":["residential","mobile"]},{"slug":"b","kind":"residential"}]}`)
		case "/v1/countries":
			fmt.Fprint(w, `{"countries":[{"countryCode":"DE"}]}`)
		case "/v1/rows":
			fmt.Fprintf(w, `{"range":%q,"networks":[{"slug":"a","listRate":1.5,"tagline":"x","kind":"residential"},{"slug":"b","listRate":2,"tagline":null,"kind":"mobile"}]}`, r.URL.Query().Get("range"))
		case "/v1/networks/a", "/v1/networks/b":
			fmt.Fprint(w, `{"profile":{"pricing":{"listRate":null},"targets":[{"target":"t1","rate":null}],"na":["pricing.listRate","targets[t1].rate"]},"stats":{"24h":{"x":1},"7d":{"x":null}}}`)
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(ts.Close)
	return ts.URL
}

func fixtureManifest() *Manifest {
	api := func(id, request, path string) Gap {
		return Gap{ID: id, Page: 1, Class: "api", Work: "W2", Issue: 710, Checks: []Check{{Request: request, Paths: []string{path}}}}
	}
	return &Manifest{Gaps: []Gap{
		api("PASS", "/v1/rows?range={range}", "networks[].listRate"),
		api("MISSING", "/v1/rows?range={range}", "networks[].cost"),
		api("NA", "/v1/networks/{slug}?kind={kind}", "profile.pricing.listRate"),
		api("NULL", "/v1/rows?range={range}", "networks[].tagline"),
		{ID: "SITE", Page: 1, Class: "website", Reason: "Website change | no API change."},
	}}
}

func TestEachGapPassesOrFailsOnARunningServer(t *testing.T) {
	r, err := CheckGaps(context.Background(), gapServer(t)+"/", fixtureManifest())
	if err != nil {
		t.Fatal(err)
	}
	if r.Revision != "abc" || r.Passed != 2 || r.Failed != 2 || r.Listed != 1 {
		t.Fatalf("report %+v", r)
	}
	want := map[string]string{"PASS": "pass", "MISSING": "fail", "NA": "pass", "NULL": "fail", "SITE": ""}
	for _, g := range r.Gaps {
		if g.Verdict != want[g.ID] {
			t.Errorf("%s: verdict %q, want %q (%+v)", g.ID, g.Verdict, want[g.ID], g.Failures)
		}
	}
	missing, null := r.Gaps[1], r.Gaps[3]
	if missing.Requests != 3 || len(missing.Failures) != 1 || missing.Failures[0] != (GapFailure{Request: "/v1/rows?range=24h", Path: "networks[].cost", Why: "missing"}) {
		t.Errorf("missing: %+v", missing)
	}
	if len(null.Failures) != 1 || null.Failures[0].Why != "null at networks[].tagline, not listed in na" {
		t.Errorf("null: %+v", null.Failures)
	}
	if n := r.Gaps[2].Requests; n != 3 {
		t.Errorf("network check: %d requests, want a and b's kinds", n)
	}
	md := string(r.Markdown())
	for _, s := range []string{"2 pass, 2 fail, 1 listed", "| NULL | 1 | api | W2 (#710) | fail | 3 requests<br>`/v1/rows?range=24h` networks[].tagline: null at",
		`| SITE | 1 | website |  |  | Website change \| no API change. |`} {
		if !strings.Contains(md, s) {
			t.Errorf("gaps.md lacks %q:\n%s", s, md)
		}
	}
}

func TestARequestThatFailsFailsItsGap(t *testing.T) {
	m := &Manifest{Gaps: []Gap{{ID: "G", Class: "api", Checks: []Check{{Request: "/v1/none?range={range}", Paths: []string{"x"}}}},
		{ID: "CC", Class: "api", Checks: []Check{{Request: "/v1/rows?range={range}&country={cc}", Paths: []string{"range"}}}}}}
	base := gapServer(t)
	r, err := CheckGaps(context.Background(), base, m)
	if err != nil {
		t.Fatal(err)
	}
	if f := r.Gaps[0].Failures; r.Gaps[0].Verdict != "fail" || len(f) != 1 || !strings.Contains(f[0].Why, "answers 404") {
		t.Errorf("a 404: %+v", r.Gaps[0])
	}
	if r.Gaps[1].Verdict != "pass" {
		t.Errorf("a country check: %+v", r.Gaps[1])
	}
	if _, err := CheckGaps(context.Background(), base+"/nowhere", m); err == nil {
		t.Error("a server without /v1/version: no error")
	}
}

func TestExpandCheck(t *testing.T) {
	sh := Shape{Networks: []Network{{Slug: "a", Kinds: []string{"residential", "mobile"}}, {Slug: "b", Kinds: []string{"residential"}}}, Country: "DE"}
	cases := map[string][]string{
		"/v1/meta":                        {"/v1/meta"},
		"/v1/home?range={range}":          {"/v1/home?range=24h", "/v1/home?range=7d", "/v1/home?range=30d"},
		"/v1/targets?kind={kind}":         {"/v1/targets?kind=residential", "/v1/targets?kind=mobile"},
		"/v1/networks/{slug}/reviews":     {"/v1/networks/a/reviews", "/v1/networks/b/reviews"},
		"/v1/networks/{slug}?kind={kind}": {"/v1/networks/a?kind=residential", "/v1/networks/a?kind=mobile", "/v1/networks/b?kind=residential"},
		"/v1/events?country={cc}":         {"/v1/events?country=DE"},
	}
	for request, want := range cases {
		if got := ExpandCheck(request, sh); !slices.Equal(got, want) {
			t.Errorf("%s: %v, want %v", request, got, want)
		}
	}
	sh.Country = ""
	if got := ExpandCheck("/v1/events?country={cc}", sh); got != nil {
		t.Errorf("no country: %v", got)
	}
	if f := checkRequests(Check{Request: "/v1/events?country={cc}", Paths: []string{"x"}}, nil, nil); len(f) != 1 || !strings.Contains(f[0].Why, "no request") {
		t.Errorf("no request: %+v", f)
	}
}

func TestCheckPath(t *testing.T) {
	var body any
	err := json.Unmarshal([]byte(`{"rows":[{"a":1,"b":null,"c":{"d":null}},{"a":2,"b":3}],"empty":[],"cells":{"x":{"e":{"chrome":{"s":"ranked"}}},"y":{}},
		"keyed":{"targets":[{"target":"t1","v":null}],"na":["targets[t1].v"]},"num":5,"na":["rows[].c.d"],
		"listed":{"profiles":[],"m":{},"na":["profiles","m"]},"reasons":{"na":["x"],"naReason":"catalog","naReasons":{"f":"catalog"}}}`), &body)
	if err != nil {
		t.Fatal(err)
	}
	cases := map[string]string{
		"rows[].a":             "",
		"rows[].b":             "null at rows[].b, not listed in na",
		"rows[].b?":            "",
		"rows[].c.d":           "", // in the root's na
		"rows[].c.d.e":         "", // a null ancestor in na
		"rows[].z":             "missing",
		"empty[].a":            "missing (empty at empty, not listed in na)",
		"listed.profiles[].id": "", // an empty list listed in na
		"listed.m.*.s":         "",
		"listed.x[].id":        "missing",
		"reasons.*.f":          "missing", // * skips na, naReason and naReasons
		"empty":                "",
		"cells.*.e.*.s":        "", // y has no e: one place is enough
		"cells.*.f":            "missing",
		"keyed.targets[].v":    "",
		"num[]":                "missing",
		"num.x":                "missing",
		"num.*":                "missing",
	}
	for path, want := range cases {
		if got := checkPath(body, path); got != want {
			t.Errorf("%s: %q, want %q", path, got, want)
		}
	}
	matches := map[string]string{"rows[].a": "^[12]$", "cells.*.e.*.s": "^ranked$", "rows[].b?": "^3$"}
	for path, re := range matches {
		if got := matchPath(body, path, re); got != "" {
			t.Errorf("%s ~ %s: %q", path, re, got)
		}
	}
	if got := matchPath(body, "rows[].a", "^1$"); got != `rows[].a is "2", not ^1$` {
		t.Errorf("a mismatch: %q", got)
	}
	if got := matchPath(body, "rows[].a", "("); !strings.Contains(got, "missing closing )") {
		t.Errorf("a bad regexp: %q", got)
	}
}

func TestLoadManifest(t *testing.T) {
	dir := t.TempDir()
	write := func(body string) string {
		p := filepath.Join(dir, "m.json")
		if err := os.WriteFile(p, []byte(body), 0o600); err != nil {
			t.Fatal(err)
		}
		return p
	}
	if m, err := LoadManifest(filepath.Join("..", "..", "..", "docs", "api-web-map-gaps.json")); err != nil || len(m.Gaps) < 200 {
		t.Fatalf("the repository's manifest: %v", err)
	}
	for body, want := range map[string]string{
		`{"gaps":[{"id":"A","class":"api"}]}`:                                                                "has no check",
		`{"gaps":[{"id":"A","class":"api","checks":[{"request":"/v1/x"}]}]}`:                                 "for no path",
		`{"gaps":[{"id":"A","class":"api","checks":[{"request":"/v1/x","paths":["a"],"match":{"a":"("}}]}]}`: "match a",
		`{"gaps":`: "unexpected end",
	} {
		if _, err := LoadManifest(write(body)); err == nil || !strings.Contains(err.Error(), want) {
			t.Errorf("%s: %v, want %q", body, err, want)
		}
	}
	if _, err := LoadManifest(filepath.Join(dir, "none.json")); err == nil {
		t.Error("a missing manifest: no error")
	}
}

// The repository's manifest runs whole against a fixture build: every api
// gap gets a verdict from real requests.
func TestTheRepositoryManifestAgainstAFixtureBuild(t *testing.T) {
	m, err := LoadManifest(filepath.Join("..", "..", "..", "docs", "api-web-map-gaps.json"))
	if err != nil {
		t.Fatal(err)
	}
	r, err := CheckGaps(context.Background(), serve(t, func(h http.Handler) http.Handler { return h }), m)
	if err != nil {
		t.Fatal(err)
	}
	for _, g := range r.Gaps {
		if (g.Class == "api") != (g.Verdict != "") || (g.Class == "api" && g.Requests == 0) {
			t.Errorf("%s: class %s, verdict %q, %d requests", g.ID, g.Class, g.Verdict, g.Requests)
		}
	}
	if r.Passed == 0 || r.Failed == 0 || r.Passed+r.Failed+r.Listed != len(m.Gaps) {
		t.Errorf("passed %d, failed %d, listed %d of %d", r.Passed, r.Failed, r.Listed, len(m.Gaps))
	}
	dir := filepath.Join(t.TempDir(), "out")
	if err := r.Write(dir); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"gaps.json", "gaps.md"} {
		if fi, err := os.Stat(filepath.Join(dir, name)); err != nil || fi.Mode().Perm() != 0o600 {
			t.Errorf("%s: %v %v", name, fi, err)
		}
	}
}

// fleetSource is a build of production's 18 networks with browser targets in
// memory, without catalog rows (as staging's); the gaps do not depend on how
// many countries a network has, so it has 10.
type fleetSource struct{ source }

func (fleetSource) Load(context.Context) (*store.Inputs, error) {
	return testscale.NetworkInputs(testscale.NetworkShape{Shape: testscale.Shape{Networks: 18, Countries: 1, Exits: 10, Tail: 10,
		Events: 50, Browser: true}, Requested: 10})
}

// The 46 gaps PERF-REPORT-1 (#720) found failing on staging's build 4, all
// "null not listed in na" or "missing" (API-NA-2, #798), pass against a build
// of production's 18 networks: with a value, or null (or empty) and listed in na.
func TestThe720GapFailuresPassForTheProductionFleet(t *testing.T) {
	t.Parallel()
	failed := strings.Fields(`HOME-15 HOME-17 HOME-18 HOME-24 HOME-25 HOME-26 HOME-27 HOME-31 HOME-36 HC-1 HC-5 TGT-1 TGT-3
		TGT-5 TGT-6 TGT-7 TGT-12 NETS-1 NET-LISTRATE NET-LEADS NET-PROTOCOL NET-RANK-PRICE NET-RANK-AVAIL NET-TARGETS-RANGE
		NET-TARGET-META NET-TARGET-RANK NET-TARGET-LATRANK NET-TRIAL-BOOL NET-VALUE-RANK NET-SCOPE-COPY NET-ALT-PRICE
		NET-VERIFY-LABEL NET-SCOPE-UNUSED CMP-6 CMP-8 CMP-9 CMP-11 CMP-12 OVL-1 TC-1 TC-2 TC-3 TC-4 TC-5 TC-6 TC-8`)
	m, err := LoadManifest(filepath.Join("..", "..", "..", "docs", "api-web-map-gaps.json"))
	if err != nil {
		t.Fatal(err)
	}
	s := server.New(fleetSource{}, server.Options{FilesDir: t.TempDir()})
	if _, err := s.Refresh(context.Background(), true); err != nil {
		t.Fatal(err)
	}
	ts := httptest.NewServer(s.Handler())
	t.Cleanup(ts.Close)
	r, err := CheckGaps(context.Background(), ts.URL, m)
	if err != nil {
		t.Fatal(err)
	}
	verdicts := map[string]GapResult{}
	for _, g := range r.Gaps {
		verdicts[g.ID] = g
	}
	for _, id := range failed {
		if g := verdicts[id]; g.Verdict != "pass" {
			t.Errorf("%s: %q %+v", id, g.Verdict, g.Failures)
		}
	}
}
