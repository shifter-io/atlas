package server

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"maps"
	"os"
	"runtime"
	"sort"
	"sync"
	"testing"
	"time"

	"github.com/Zeus-Development/proxy-index/api/internal/build"
	"github.com/Zeus-Development/proxy-index/api/internal/store"
	"github.com/Zeus-Development/proxy-index/api/internal/testdb"
	"github.com/Zeus-Development/proxy-index/api/internal/testscale"
)

// v4ScaleGolden holds, per TestEveryV4Answer case, the SHA-256 of every
// answer of its v4 build, written by that test with GOLDEN_WRITE=<path>.
const v4ScaleGolden = "testdata/answers-v4-scale.json"

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

// payloadGroups is a payload's product groups, as testscale.AsV4Answers
// takes them.
func payloadGroups(t *testing.T, payload []byte) [][]byte {
	t.Helper()
	var s struct {
		Products []json.RawMessage `json:"products"`
	}
	if err := json.Unmarshal(payload, &s); err != nil {
		t.Fatal(err)
	}
	out := make([][]byte, len(s.Products))
	for i, g := range s.Products {
		out[i] = g
	}
	return out
}

// sameV4Answers reports each answer of got that differs from want, around
// its first difference, the first ten at most.
func sameV4Answers(t *testing.T, want, got map[string][]byte) {
	t.Helper()
	if len(want) < 20 || len(got) != len(want) {
		t.Fatalf("%d answers, want %d", len(got), len(want))
	}
	names := make([]string, 0, len(want))
	for name := range want {
		names = append(names, name)
	}
	sort.Strings(names)
	differ := 0
	for _, name := range names {
		w, g := want[name], got[name]
		if bytes.Equal(w, g) {
			continue
		}
		if differ++; differ > 10 {
			continue
		}
		i := 0
		for i < min(len(w), len(g)) && w[i] == g[i] {
			i++
		}
		from := max(i-120, 0)
		t.Errorf("%s differs at byte %d:\nwant …%s\ngot  …%s", name, i, w[from:min(len(w), i+200)], g[from:min(len(g), i+200)])
	}
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
// While the store still reads builds before v4, each case also stores the
// same data whole (testscale.Listed: each window's lists from the one source
// a v4 build has) in a database of its own, at once with the v4 build, and
// shows that every v4 answer is that build's after testscale.AsV4Answers,
// the dataset and details apart from the members a v4 dataset file differs
// in (v4FileKeys).
func TestEveryV4Answer(t *testing.T) {
	t.Parallel()
	raw, err := os.ReadFile(v4ScaleGolden)
	write := os.Getenv("GOLDEN_WRITE")
	if err != nil && write == "" {
		t.Fatal(err)
	}
	want := map[string]map[string]string{}
	if write == "" {
		if err := json.Unmarshal(raw, &want); err != nil {
			t.Fatal(err)
		}
	}
	var mu sync.Mutex
	got := map[string]map[string]string{}
	t.Run("case", func(t *testing.T) {
		for _, c := range v4ScaleCases {
			t.Run(c.name, func(t *testing.T) {
				t.Parallel()
				if c.name == "QA-1" && testing.Short() {
					t.Skip("stores a QA-1-sized build twice")
				}
				answers := v4ScaleAnswers(t, c)
				mu.Lock()
				got[c.name] = answers
				mu.Unlock()
				if write == "" {
					sameSums(t, want[c.name], answers)
				}
			})
		}
	})
	if write != "" && !t.Failed() {
		b, err := json.MarshalIndent(got, "", " ")
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(write, append(b, '\n'), 0o644); err != nil {
			t.Fatal(err)
		}
	}
}

// v4ScaleCase is a TestEveryV4Answer payload, with the answers rendered on
// request.
type v4ScaleCase struct {
	name    string
	payload func() ([]byte, error)
	pages   []string
}

var v4ScaleCases = []v4ScaleCase{
	{"every-request", testscale.EveryRequest, goldenPages},
	{"QA-1", qa1Networks, []string{"/v1/networks/net00/places?dimension=city", "/v1/networks/net00/places?dimension=asn&range=7d&page=2",
		"/v1/networks/net03/places?dimension=region&q=region+1", "/v1/countries/AA/cities", "/v1/countries/AB/cities?q=city+1",
		"/v1/networks/all/places?dimension=city&range=24h", "/v1/search?q=net"}},
}

// v4ScaleAnswers stores the case as a v4 build and, before v4 (whole,
// Listed), in a database of its own each, both at once (build/v4 and
// build/whole); checks that the v4 answers are the whole build's after
// AsV4Answers and returns their SHA-256. Both databases are made first, so
// without a database the case skips before it builds a payload.
func v4ScaleAnswers(t *testing.T, c v4ScaleCase) map[string]string {
	t.Helper()
	v4DB, wholeDB := testdb.New(t), testdb.New(t)
	payload, err := c.payload()
	if err != nil {
		t.Fatal(err)
	}
	var v4, whole map[string][]byte
	t.Run("build", func(t *testing.T) {
		t.Run("v4", func(t *testing.T) {
			t.Parallel()
			v4 = renderedAnswers(t, v4DB, v4DB.PublishV4(t, payload), c.pages, asBuilt)
		})
		t.Run("whole", func(t *testing.T) {
			t.Parallel()
			listed, err := testscale.Listed(payload)
			if err != nil {
				t.Fatal(err)
			}
			whole = renderedAnswers(t, wholeDB, wholeDB.Publish(t, listed), c.pages, asBuilt)
		})
	})
	if t.Failed() {
		return nil
	}
	want, err := testscale.AsV4Answers(whole, payloadGroups(t, payload))
	if err != nil {
		t.Fatal(err)
	}
	sameV4Answers(t, withoutV4File(t, want), withoutV4File(t, v4))
	return sums(v4)
}

// v4FileKeys are the members a v4 build's dataset file and details differ
// in by design (README, the dataset file): a v4 build has no
// coverageEvidence, has its lists' first pages in lists, and has the country
// answers in pages as well (each compared as its own answer, as are the
// other pages, the pages answer).
var v4FileKeys = map[string]bool{"coverageEvidence": true, "lists": true, "pages": true}

// withoutV4File is the answers with the dataset and details without
// v4FileKeys and without /v1/version, whose revision is the dataset's hash.
func withoutV4File(t *testing.T, answers map[string][]byte) map[string][]byte {
	t.Helper()
	out := maps.Clone(answers)
	delete(out, versionPath)
	for _, name := range []string{"dataset", "details"} {
		out[name] = withoutKeys(t, out[name], v4FileKeys)
	}
	return out
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
