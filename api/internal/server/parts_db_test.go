package server

import (
	"context"
	"crypto/sha256"
	"fmt"
	"os"
	"runtime"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/Zeus-Development/proxy-index/api/internal/build"
	"github.com/Zeus-Development/proxy-index/api/internal/store"
	"github.com/Zeus-Development/proxy-index/api/internal/testdb"
	"github.com/Zeus-Development/proxy-index/api/internal/testscale"
)

// answers loads the database's current build, renders it and returns the
// SHA-256 of every answer: each static one, the dataset, the details and
// pages the per-page answers come from, and the given answers rendered on
// request (status and body). The content hash and build time are set to one
// value, so two builds of the same payload compare.
func answers(t *testing.T, db *testdb.DB, id int64, pages []string) map[string][32]byte {
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
	v, err := RenderVersion(1, res, Options{FilesDir: t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	out := map[string][32]byte{"dataset": sha256.Sum256(fileBytes(t, v.Dataset))}
	for p, r := range v.Static {
		out[p] = sha256.Sum256(r.Identity)
	}
	for name, part := range map[string]any{"details": res.Details, "pages": res.Pages} {
		b, err := Marshal(part)
		if err != nil {
			t.Fatal(err)
		}
		out[name] = sha256.Sum256(b)
	}
	srv := New(&fakeSource{}, Options{})
	srv.current.Store(v)
	h := srv.Handler()
	for _, path := range pages {
		rec := get(h, path)
		out["GET "+path] = sha256.Sum256(fmt.Appendf(nil, "%d %s", rec.Code, rec.Body.Bytes()))
	}
	return out
}

// Every answer is byte for byte the same whether the build is stored whole
// or in parts with its daily groups' 24-hour series sparse: the every-request
// fixture (a census/pulse group and a daily copy) and a QA-1-sized build
// (18 networks x 40 requested countries, every group daily). A QA-1-sized
// payload is more than one jsonb row holds (why builds are stored in parts),
// so its whole form is the same parts with every point kept (both from
// testscale.CurrentGroups, which splits the payload as it builds it); the
// every-request case and store's TestPartsReadAsTheSinglePayload show those
// read as the single row does. Each form is stored in its own database and
// read in its own subtest, both at once (form/whole and form/parts); then
// the case compares their answers. Both databases are made first, so without
// a database the case skips (testdb.New) before it builds a payload, and never
// compares the answers of forms that did not run.
func TestEveryAnswerIsTheSameFromParts(t *testing.T) {
	t.Parallel()
	for _, c := range []struct {
		name  string
		form  func() (storedForm, error)
		pages []string // answers rendered on request
	}{
		{"every-request", func() (storedForm, error) {
			payload, err := testscale.EveryRequest()
			return storedForm{single: payload, parts: func(sparse bool) ([]byte, [][]byte, [][]byte, error) {
				return testscale.Parts(payload, sparse)
			}}, err
		}, []string{"/v1/networks/shifter/places?dimension=city", "/v1/networks/shifter/places?dimension=asn&range=7d",
			"/v1/countries/US/cities", "/v1/networks/all/places?dimension=city&range=24h", "/v1/search?q=shift", "/v1/events"}},
		{"QA-1", func() (storedForm, error) {
			g, err := testscale.CurrentGroups(testscale.QA1)
			if err != nil {
				return storedForm{}, err
			}
			return storedForm{parts: g.Parts}, nil
		}, []string{"/v1/networks/net00/places?dimension=city", "/v1/networks/net00/places?dimension=asn&range=7d&page=2",
			"/v1/networks/net03/places?dimension=region&q=region+1", "/v1/countries/AA/cities", "/v1/countries/AB/cities?q=city+1",
			"/v1/networks/all/places?dimension=city&range=24h", "/v1/search?q=net"}},
	} {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			if c.name == "QA-1" && testing.Short() {
				t.Skip("stores a QA-1-sized build twice")
			}
			wholeDB, partsDB := testdb.New(t), testdb.New(t)
			form, err := c.form()
			if err != nil {
				t.Fatal(err)
			}
			var whole, parts map[string][32]byte
			t.Run("form", func(t *testing.T) {
				t.Run("whole", func(t *testing.T) {
					t.Parallel()
					whole = formAnswers(t, wholeDB, form, false, c.pages)
				})
				t.Run("parts", func(t *testing.T) {
					t.Parallel()
					parts = formAnswers(t, partsDB, form, true, c.pages)
				})
			})
			if t.Failed() {
				return
			}
			sameAnswers(t, whole, parts)
			if status, err := os.ReadFile("/proc/self/status"); err == nil {
				for _, line := range strings.Split(string(status), "\n") {
					if strings.HasPrefix(line, "VmHWM:") {
						t.Logf("peak resident memory of the test binary: %s", strings.TrimSpace(strings.TrimPrefix(line, "VmHWM:")))
					}
				}
			}
		})
	}
}

// storedForm is a case's payload as each form stores it: single, when set,
// is its whole form as one payload row; parts is it in parts, with sparse
// the daily groups' 24-hour series sparse (testscale.Parts).
type storedForm struct {
	single []byte
	parts  func(sparse bool) (head []byte, products, runs [][]byte, err error)
}

// formAnswers stores the form in db, a database of its own, whole (as one
// row when f.single is set) or with sparse in parts, and returns its answers.
func formAnswers(t *testing.T, db *testdb.DB, f storedForm, sparse bool, pages []string) map[string][32]byte {
	t.Helper()
	if f.single != nil && !sparse {
		return answers(t, db, db.Publish(t, f.single), pages)
	}
	head, products, runs, err := f.parts(sparse)
	if err != nil {
		t.Fatal(err)
	}
	return answers(t, db, db.PublishParts(t, head, products, runs), pages)
}

func sameAnswers(t *testing.T, whole, parts map[string][32]byte) {
	t.Helper()
	if len(whole) < 20 || len(whole) != len(parts) {
		t.Fatalf("%d answers stored whole, %d from parts", len(whole), len(parts))
	}
	paths := make([]string, 0, len(whole))
	for p := range whole {
		paths = append(paths, p)
	}
	sort.Strings(paths)
	differ := 0
	for _, p := range paths {
		if b, ok := parts[p]; !ok || b != whole[p] {
			if differ++; differ <= 10 {
				t.Errorf("%s differs from parts", p)
			}
		}
	}
}
