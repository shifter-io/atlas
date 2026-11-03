package server

import (
	"encoding/json"
	"fmt"
	"net/http"
	"path/filepath"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/Zeus-Development/proxy-index-website/api/internal/build"
	"github.com/Zeus-Development/proxy-index-website/api/internal/testscale"
)

// scaleVersion renders a QA-1-sized build (18 networks x 40 requested
// countries, three windows each) once per test binary.
func scaleVersion(t *testing.T) (*Version, *build.Result, time.Duration) {
	t.Helper()
	if testing.Short() {
		t.Skip("renders a QA-1-sized build")
	}
	in, err := testscale.Inputs(testscale.QA1)
	if err != nil {
		t.Fatal(err)
	}
	// Guides and reviews like those the served build indexes (fixtures/content
	// keeps the website's as test input; serve reads --content-dir).
	content, err := build.ReadContent(filepath.Join("..", "..", "..", "fixtures", "content"))
	if err != nil {
		t.Fatal(err)
	}
	t0 := time.Now()
	res, err := build.Build(in, build.Options{Content: content})
	if err != nil {
		t.Fatal(err)
	}
	v, err := RenderVersion(1, res, Options{})
	if err != nil {
		t.Fatal(err)
	}
	return v, res, time.Since(t0)
}

// TestEveryAnswerFitsTheCap: every answer of a QA-1-sized build, and every
// page of its long lists, is at most build.MaxAnswerBytes. The dataset is not
// an answer: it is the download file /v1/dataset redirects to.
func TestEveryAnswerFitsTheCap(t *testing.T) {
	v, res, took := scaleVersion(t)
	// The scale build must reproduce the answer that once broke the cap: the
	// whole coverage report (324,621 bytes at QA-1; about 240 KB here, since
	// the synthetic groups are more complete than QA-1's). Served whole it
	// would fail this test; /v1/coverage now serves its index and one
	// /v1/coverage/{slug} per provider.
	whole, err := Marshal(res.Coverage)
	if err != nil {
		t.Fatal(err)
	}
	if len(whole) <= build.MaxAnswerBytes {
		t.Errorf("the scale build's whole coverage report is %d bytes, within the cap: it no longer reproduces QA-1 (324,621)", len(whole))
	}
	paths := make([]string, 0, len(v.Static))
	for p := range v.Static {
		paths = append(paths, p)
	}
	sort.Strings(paths)
	largest := map[string]int{}
	for _, p := range paths {
		n := len(v.Static[p].Identity)
		group := p
		if i := strings.LastIndex(p, "/"); strings.Count(p, "/") > 2 {
			group = p[:i] + "/*"
		}
		largest[group] = max(largest[group], n)
		if n > build.MaxAnswerBytes {
			t.Errorf("%s is %d bytes, above the %d-byte cap", p, n, build.MaxAnswerBytes)
		}
	}
	srv := New(&fakeSource{}, Options{})
	srv.current.Store(v)
	h := srv.Handler()
	for _, path := range []string{"/v1/networks/net00/places?dimension=city", "/v1/networks/net00/places?dimension=asn&range=7d&page=2",
		"/v1/networks/net00/places?dimension=region&q=region+1", "/v1/countries/AA/cities", "/v1/countries/AB/cities?q=city+1",
		"/v1/networks/all/places?dimension=city&range=24h"} {
		rec := get(h, path)
		if rec.Code != http.StatusOK {
			t.Fatalf("%s: %d %s", path, rec.Code, rec.Body)
		}
		var page build.PlacePage
		if err := json.Unmarshal(rec.Body.Bytes(), &page); err != nil || len(page.Rows) == 0 || len(page.Rows) > build.PageSize {
			t.Errorf("%s: %d rows (%v)", path, len(page.Rows), err)
		}
		largest["page "+strings.SplitN(path, "?", 2)[0]] = max(largest["page "+strings.SplitN(path, "?", 2)[0]], rec.Body.Len())
		if rec.Body.Len() > build.MaxAnswerBytes {
			t.Errorf("%s is %d bytes, above the cap", path, rec.Body.Len())
		}
	}
	if r := getOnce(h, "/v1/dataset"); r.Code != http.StatusFound || r.Body.Len() > 0 {
		t.Errorf("/v1/dataset: %d, %d bytes", r.Code, r.Body.Len())
	}
	report := []string{fmt.Sprintf("build+render %v, %d answers, whole coverage %d, dataset file %d (gzip %d, br %d)", took.Round(time.Millisecond),
		len(paths), len(whole), len(v.Dataset.Identity), len(v.Dataset.Gzip), len(v.Dataset.Brotli))}
	for g, n := range largest {
		report = append(report, fmt.Sprintf("%-48s %9d", g, n))
	}
	sort.Strings(report[1:])
	t.Log("largest answer per path:\n" + strings.Join(report, "\n"))
}
