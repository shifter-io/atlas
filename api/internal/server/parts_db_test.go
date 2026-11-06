package server

import (
	"context"
	"crypto/sha256"
	"fmt"
	"os"
	"runtime"
	"runtime/debug"
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
// so its whole form is the same parts with every point kept; the
// every-request case and store's TestPartsReadAsTheSinglePayload show those
// read as the single row does.
func TestEveryAnswerIsTheSameFromParts(t *testing.T) {
	db := testdb.New(t)
	for _, c := range []struct {
		name    string
		payload func() ([]byte, error)
		single  bool     // the whole form is one payload row
		pages   []string // answers rendered on request
	}{
		{"every-request", testscale.EveryRequest, true, []string{"/v1/networks/shifter/places?dimension=city", "/v1/networks/shifter/places?dimension=asn&range=7d",
			"/v1/countries/US/cities", "/v1/networks/all/places?dimension=city&range=24h", "/v1/search?q=shift", "/v1/events"}},
		{"QA-1", func() ([]byte, error) {
			in, err := testscale.Inputs(testscale.QA1)
			if err != nil {
				return nil, err
			}
			return testscale.Current(in.Snapshot.Payload)
		}, false, []string{"/v1/networks/net00/places?dimension=city", "/v1/networks/net00/places?dimension=asn&range=7d&page=2",
			"/v1/networks/net03/places?dimension=region&q=region+1", "/v1/countries/AA/cities", "/v1/countries/AB/cities?q=city+1",
			"/v1/networks/all/places?dimension=city&range=24h", "/v1/search?q=net"}},
	} {
		t.Run(c.name, func(t *testing.T) {
			if c.name == "QA-1" {
				if testing.Short() {
					t.Skip("stores a QA-1-sized build twice")
				}
				// Two QA-1-sized builds read and rendered one after the
				// other: collect sooner than the default, so the test fits
				// an 8 GB machine beside its database.
				defer debug.SetMemoryLimit(debug.SetMemoryLimit(3 << 30))
			}
			payload, err := c.payload()
			if err != nil {
				t.Fatal(err)
			}
			var id int64
			if c.single {
				id = db.Publish(t, payload)
			} else {
				head, products, runs, err := testscale.Parts(payload, false)
				if err != nil {
					t.Fatal(err)
				}
				id = db.PublishParts(t, head, products, runs)
			}
			whole := answers(t, db, id, c.pages)
			head, products, runs, err := testscale.Parts(payload, true)
			if err != nil {
				t.Fatal(err)
			}
			id = db.PublishParts(t, head, products, runs)
			sameAnswers(t, whole, answers(t, db, id, c.pages))
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
