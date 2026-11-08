package server

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"slices"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/Zeus-Development/proxy-index/api/internal/build"
	"github.com/Zeus-Development/proxy-index/api/internal/store"
	"github.com/Zeus-Development/proxy-index/api/internal/testdb"
	"github.com/Zeus-Development/proxy-index/api/internal/testscale"
)

// v4ScaleGolden holds, per TestEveryV4Answer case, the SHA-256 of every
// answer of its v4 build: one file per family (goldenFamily), each holding
// its answers per case, written by that test with GOLDEN_WRITE=1.
const v4ScaleGolden = "testdata/answers-v4-scale"

// goldenFamilies are the families of the answers served before the page
// answer registry; a registered answer's family is its name.
var goldenFamilies = []string{"rankings", "networks", "charts", "countries", "places", "coverage", "search", "events", "dataset"}

// goldenFamily is the family of an answer's golden name: a stored path, a
// request rendered on request ("GET <url>"), or dataset, pages or details.
func goldenFamily(name string) string {
	path, get := strings.CutPrefix(name, "GET ")
	for _, a := range build.Answers() {
		if !get && a.Owns(path) || get && slices.Contains(a.Samples, path) {
			return a.Name
		}
	}
	path, _, _ = strings.Cut(path, "?")
	parts := strings.Split(path, "/") // "", "v1", the family, ...
	switch {
	case name == "dataset" || name == "pages" || name == "details" || path == versionPath:
		return "dataset"
	case len(parts) < 3:
		return ""
	case get && (parts[2] == "places" || strings.HasSuffix(path, "/places") || strings.HasSuffix(path, "/cities") ||
		strings.HasSuffix(path, "/regions") || strings.HasSuffix(path, "/coverage")):
		return "places"
	case parts[2] == "search-index":
		return "search"
	}
	return parts[2]
}

// readV4Golden reads every family file of v4ScaleGolden, by case, and fails
// when an answer is in another family's file.
func readV4Golden(t *testing.T) map[string]map[string]string {
	t.Helper()
	files, err := filepath.Glob(filepath.Join(v4ScaleGolden, "*.json"))
	if err != nil || len(files) == 0 {
		t.Fatalf("no golden files in %s (%v)", v4ScaleGolden, err)
	}
	out := map[string]map[string]string{}
	for _, f := range files {
		raw, err := os.ReadFile(f)
		if err != nil {
			t.Fatal(err)
		}
		var cases map[string]map[string]string
		if err := json.Unmarshal(raw, &cases); err != nil {
			t.Fatalf("%s: %v", f, err)
		}
		family := strings.TrimSuffix(filepath.Base(f), ".json")
		for c, answers := range cases {
			if out[c] == nil {
				out[c] = map[string]string{}
			}
			for name, sum := range answers {
				if got := goldenFamily(name); got != family {
					t.Errorf("%s is in %s, its family is %s", name, f, got)
				}
				out[c][name] = sum
			}
		}
	}
	return out
}

// writeV4Golden writes got, by case, as one file per family.
func writeV4Golden(t *testing.T, got map[string]map[string]string) {
	t.Helper()
	known := map[string]bool{}
	for _, f := range goldenFamilies {
		known[f] = true
	}
	for _, a := range build.Answers() {
		known[a.Name] = true
	}
	files := map[string]map[string]map[string]string{}
	for c, answers := range got {
		for name, sum := range answers {
			family := goldenFamily(name)
			if !known[family] {
				t.Fatalf("%s has no golden family (%q)", name, family)
			}
			if files[family] == nil {
				files[family] = map[string]map[string]string{}
			}
			if files[family][c] == nil {
				files[family][c] = map[string]string{}
			}
			files[family][c][name] = sum
		}
	}
	for family, cases := range files {
		b, err := json.MarshalIndent(cases, "", " ")
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(v4ScaleGolden, family+".json"), append(b, '\n'), 0o644); err != nil {
			t.Fatal(err)
		}
	}
}

// renderedAnswers loads the database's current build, applies edit to the
// built result, renders it and returns every answer: each static one, the
// dataset, the details and pages the per-page answers come from, and the
// given answers rendered on request (status and body). The content hash and
// build time are set to one value, so two builds of the same data compare.
func renderedAnswers(t *testing.T, db *testdb.DB, id int64, pages []string, edit func(*build.Result)) map[string][]byte {
	t.Helper()
	defer runtime.GC()
	db.Exec(t, "UPDATE calculated_snapshots SET content_hash='same' WHERE build_id=$1", id)
	ctx := context.Background()
	pool, err := store.OpenReadOnly(ctx, db.DSN)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	in, err := store.Load(ctx, pool)
	if err != nil {
		t.Fatal(err)
	}
	if in.Snapshot == nil || in.Snapshot.BuildID != id {
		t.Fatalf("read %+v, want build %d", in.Snapshot, id)
	}
	res, err := build.Build(in)
	if err != nil {
		t.Fatal(err)
	}
	res.BuiltAt = time.Date(2026, 11, 4, 0, 0, 0, 0, time.UTC)
	res.Coverage.BuiltAt = build.JSISO(res.BuiltAt)
	edit(res)
	v, err := RenderVersion(1, res, Options{FilesDir: t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	out := map[string][]byte{"dataset": fileBytes(t, v.Dataset)}
	for p, r := range v.Static {
		out[p] = r.Identity
	}
	for name, part := range map[string]any{"details": res.Details, "pages": res.Pages} {
		if out[name], err = Marshal(part); err != nil {
			t.Fatal(err)
		}
	}
	srv := New(&fakeSource{}, Options{})
	srv.current.Store(v)
	h := srv.Handler()
	for _, path := range pages {
		rec := get(h, path)
		out["GET "+path] = fmt.Appendf(nil, "%d %s", rec.Code, rec.Body.Bytes())
	}
	return out
}

// asBuilt leaves a built result as it is.
func asBuilt(*build.Result) {}

// sums is the hex SHA-256 of every answer.
func sums(answers map[string][]byte) map[string]string {
	out := make(map[string]string, len(answers))
	for name, b := range answers {
		h := sha256.Sum256(b)
		out[name] = hex.EncodeToString(h[:])
	}
	return out
}

// qa1Networks is the QA-1 build as payload v4 publishes it, as a payload
// before v4 (as Current writes it): testscale.QA1Networks' 18 networks, one
// product group each with its own requested country, so PublishV4 makes
// one network of each.
func qa1Networks() ([]byte, error) {
	in, err := testscale.Inputs(testscale.QA1Networks.Shape)
	if err != nil {
		return nil, err
	}
	return testscale.Current(in.Snapshot.Payload)
}

// Every answer of a v4 build (testdb.PublishV4), the server's database read
// of it rendered: the every-request fixture (a census/pulse group and a
// daily copy) and the QA-1 build as payload v4 (qa1Networks: 18 networks
// with every exit country, the tails and 2,000 City x ASN rows per window).
// Each answer is compared with the per-case SHA-256 golden v4ScaleGolden.
func TestEveryV4Answer(t *testing.T) {
	t.Parallel()
	write := os.Getenv("GOLDEN_WRITE") != ""
	want := map[string]map[string]string{}
	if !write {
		want = readV4Golden(t)
	}
	var mu sync.Mutex
	got := map[string]map[string]string{}
	t.Run("case", func(t *testing.T) {
		for _, c := range v4ScaleCases {
			t.Run(c.name, func(t *testing.T) {
				t.Parallel()
				if c.name == "QA-1" && testing.Short() {
					t.Skip("stores a QA-1-sized build")
				}
				answers := v4ScaleAnswers(t, c)
				mu.Lock()
				got[c.name] = answers
				mu.Unlock()
				if !write {
					sameSums(t, want[c.name], answers)
				}
			})
		}
	})
	if write && !t.Failed() {
		writeV4Golden(t, got)
	}
}

// v4ScaleCase is a TestEveryV4Answer payload, with the answers rendered on
// request.
type v4ScaleCase struct {
	name    string
	payload func() ([]byte, error)
	pages   []string
	edit    func(*build.Result)
}

var v4ScaleCases = []v4ScaleCase{
	{"every-request", testscale.EveryRequest, append(slices.Clone(goldenPages), tapePages...), withTapeBurst},
	{"QA-1", qa1Networks, []string{"/v1/networks/net00/places?dimension=city", "/v1/networks/net00/places?dimension=asn&range=7d&page=2",
		"/v1/networks/net03/places?dimension=region&q=region+1", "/v1/countries/AA/cities", "/v1/countries/AB/cities?q=city+1",
		"/v1/networks/all/places?dimension=city&range=24h", "/v1/search?q=net"}, asBuilt},
}

// v4ScaleAnswers stores the case as a v4 build and returns the SHA-256 of
// its answers, every registered answer's samples rendered on request. The database is made first, so without one the case skips
// before it builds a payload.
func v4ScaleAnswers(t *testing.T, c v4ScaleCase) map[string]string {
	t.Helper()
	db := testdb.New(t)
	payload, err := c.payload()
	if err != nil {
		t.Fatal(err)
	}
	pages := slices.Clone(c.pages)
	for _, a := range build.Answers() {
		pages = append(pages, a.Samples...)
	}
	answers := renderedAnswers(t, db, db.PublishV4(t, payload), pages, c.edit)
	windowFiguresServed(t, answers)
	return sums(answers)
}

// sameSums reports each answer whose SHA-256 differs from the golden's.
func sameSums(t *testing.T, want, got map[string]string) {
	t.Helper()
	if len(want) < 20 || len(got) != len(want) {
		t.Fatalf("%d answers, %d in the golden", len(got), len(want))
	}
	names := make([]string, 0, len(want))
	for name := range want {
		names = append(names, name)
	}
	sort.Strings(names)
	for _, name := range names {
		if got[name] != want[name] {
			t.Errorf("%s differs from the golden", name)
		}
	}
}
