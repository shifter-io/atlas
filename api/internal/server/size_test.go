package server

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/shifter-io/atlas/api/internal/build"
	"github.com/shifter-io/atlas/api/internal/testscale"
)

// scaleVersion renders the QA-1 build as payload v4 (18 networks, each one
// summary over 40 requested countries, three windows each).
func scaleVersion(t *testing.T) (*Version, *build.Result, time.Duration) {
	t.Helper()
	if testing.Short() {
		t.Skip("renders a QA-1-sized build")
	}
	if underRace {
		// The race detector multiplies this build's memory; on the 4 GB CI
		// runner the process is killed and the job is lost. The build is
		// covered, without the detector, by the go test step.
		t.Skip("a QA-1-sized build does not fit the CI runner under -race")
	}
	in, err := testscale.NetworkInputs(testscale.QA1Networks)
	if err != nil {
		t.Fatal(err)
	}
	t0 := time.Now()
	res, err := build.Build(in)
	if err != nil {
		t.Fatal(err)
	}
	v, err := RenderVersion(1, res, Options{FilesDir: t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	return v, res, time.Since(t0)
}

// The dataset file's bounds at the QA-1 scale (18 networks). datasetMaxBytes
// was raised from 12 MiB to 13 MiB on 2026-11-12 for the success scores
// (#898) and is back at 12 MiB since #900 removed the success-rate % fields
// (2026-11-14).
const (
	datasetMaxBytes = 12 << 20
	datasetMaxGzip  = 2 << 20
)

// TestEveryAnswerFitsTheCap: every answer of a QA-1-sized build, every page
// of its long lists and every registered answer's sample is at most
// build.MaxAnswerBytes. The dataset is not an answer: it is the download
// file /v1/dataset redirects to, bounded by datasetMaxBytes and
// datasetMaxGzip.
func TestEveryAnswerFitsTheCap(t *testing.T) {
	t.Parallel()
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
	// Every registered answer's samples (api/answers/README.md).
	for _, a := range build.Answers() {
		for _, path := range a.Samples {
			rec := get(h, path)
			largest["answer "+a.Name] = max(largest["answer "+a.Name], rec.Body.Len())
			if rec.Code != http.StatusOK || rec.Body.Len() > build.MaxAnswerBytes {
				t.Errorf("answer %s: %s answers %d, %d bytes", a.Name, path, rec.Code, rec.Body.Len())
			}
		}
	}
	// The dataset file of 18 networks stays small (DATASET-SIZE-1).
	if v.Dataset.Bytes > datasetMaxBytes || v.Dataset.Gzip > datasetMaxGzip {
		t.Errorf("the dataset file is %d bytes (gzip %d), above %d (gzip %d)", v.Dataset.Bytes, v.Dataset.Gzip, datasetMaxBytes, datasetMaxGzip)
	}
	if r := getOnce(h, "/v1/dataset"); r.Code != http.StatusFound || r.Body.Len() > 0 {
		t.Errorf("/v1/dataset: %d, %d bytes", r.Code, r.Body.Len())
	}
	// The streamed file is today's encoding of the whole dataset, byte for byte.
	if want, err := Marshal(res.Dataset); err != nil || !bytes.Equal(fileBytes(t, v.Dataset), want) {
		t.Errorf("the dataset file is not the dataset's encoding (%v)", err)
	}
	report := []string{fmt.Sprintf("build+render %v, %d answers, whole coverage %d, dataset file %d (gzip %d, br %d)", took.Round(time.Millisecond),
		len(paths), len(whole), v.Dataset.Bytes, v.Dataset.Gzip, v.Dataset.Brotli)}
	for g, n := range largest {
		report = append(report, fmt.Sprintf("%-48s %9d", g, n))
	}
	sort.Strings(report[1:])
	t.Log("largest answer per path:\n" + strings.Join(report, "\n"))
}
