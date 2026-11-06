package server

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/build"
	"github.com/Zeus-Development/proxy-index/api/internal/store"
)

// freshSource serves what a fresh database holds after one real test cycle:
// the framework's provider and product, one published product group (the
// every-request fixture: Shifter residential, requested country US, exits in
// US and DE) and, unless a test adds some, no site catalog rows.
type freshSource struct {
	payload []byte
	catalog []store.ProviderRow
}

func newFreshSource(t *testing.T, catalog ...store.ProviderRow) *freshSource {
	t.Helper()
	_, file, _, _ := runtime.Caller(0)
	raw, err := os.ReadFile(filepath.Join(filepath.Dir(file), "..", "..", "fixtures", "measured", "snapshot-every-request.json"))
	if err != nil {
		t.Fatal(err)
	}
	return &freshSource{payload: raw, catalog: catalog}
}

func (f *freshSource) Fingerprint(context.Context) (store.Fingerprint, error) {
	return store.Fingerprint{SiteRevision: 1}, nil
}

func (f *freshSource) Load(context.Context) (*store.Inputs, error) {
	var doc struct {
		Targets []struct {
			ID    string `json:"id"`
			Label string `json:"label"`
		} `json:"targets"`
	}
	if err := json.Unmarshal(f.payload, &doc); err != nil {
		return nil, err
	}
	in := &store.Inputs{Snapshot: &store.SnapshotRow{BuildID: 1, ContentHash: "fresh", Payload: f.payload},
		FrameworkProviders: []string{"shifter"}, FrameworkProducts: []store.FrameworkProduct{{ID: 1, Provider: "shifter", Name: "residential"}},
		Providers: append([]store.ProviderRow{}, f.catalog...), EventsComplete: true, NotableComplete: true}
	for i, t := range doc.Targets {
		label := t.Label
		in.FrameworkTargets = append(in.FrameworkTargets, store.FrameworkTarget{ID: t.ID, Label: &label, SortOrder: int32(i)})
	}
	return in, nil
}

func (f *freshSource) LoadEvents(context.Context) (*store.Inputs, error) {
	return &store.Inputs{EventsComplete: true, NotableComplete: true}, nil
}

func (f *freshSource) Events(context.Context, store.EventFilter) ([]store.Event, error) {
	return nil, nil
}

// freshServer builds a server over src and returns its log.
func freshServer(t *testing.T, src Source) (*Server, *bytes.Buffer) {
	t.Helper()
	var log bytes.Buffer
	s := New(src, Options{Logger: slog.New(slog.NewTextHandler(&log, nil)), FilesDir: t.TempDir()})
	if _, err := s.Refresh(context.Background(), true); err != nil {
		t.Fatal(err)
	}
	return s, &log
}

func health(t *testing.T, s *Server) Health {
	t.Helper()
	rec := get(s.Handler(), "/healthz")
	var h Health
	if err := json.Unmarshal(rec.Body.Bytes(), &h); err != nil {
		t.Fatalf("healthz %d %s: %v", rec.Code, rec.Body, err)
	}
	return h
}

// FreshPages is every page a fresh database with one published product and
// no catalog rows must serve (API-ROUTE-CHECK-1).
var FreshPages = []string{"/v1/networks", "/v1/networks/shifter", "/v1/networks/shifter/chart", "/v1/networks/shifter/places",
	"/v1/countries", "/v1/countries/us", "/v1/countries/US", "/v1/rankings", "/v1/networks/all"}

func TestOnePublishedProductWithoutCatalogRowsServesEveryPage(t *testing.T) {
	t.Parallel()
	s, log := freshServer(t, newFreshSource(t))
	h := s.Handler()
	for _, p := range FreshPages {
		if rec := get(h, p); rec.Code != 200 {
			t.Errorf("%s: %d %s", p, rec.Code, rec.Body)
		}
	}
	hz := health(t, s)
	if hz.Status != "ok" || hz.Unserved == nil || len(hz.Unserved) != 0 {
		t.Fatalf("healthz status %q unserved %v", hz.Status, hz.Unserved)
	}
	if strings.Contains(log.String(), "without their network pages") {
		t.Fatalf("a served product was logged as unserved:\n%s", log)
	}
}

func TestAPublishedProductWithoutItsNetworkPagesIsFlagged(t *testing.T) {
	t.Parallel()
	// A catalog row gives the network a slug the routes refuse: the product
	// is published but its pages answer 400.
	s, log := freshServer(t, newFreshSource(t, store.ProviderRow{Slug: "Shifter_IO", Name: "Shifter", Kinds: []string{"residential"},
		FrameworkProvider: sptr("shifter"), Listed: true, Strengths: []string{}, Weaknesses: []string{}, BestFor: []string{}}))
	hz := health(t, s)
	if hz.Status != "degraded" || len(hz.Unserved) == 0 {
		t.Fatalf("healthz status %q unserved %v", hz.Status, hz.Unserved)
	}
	joined := strings.Join(hz.Unserved, "\n")
	for _, want := range []string{"/v1/networks/Shifter_IO answers 400", "/v1/networks/Shifter_IO/chart?kind=residential answers 400",
		"/v1/networks/Shifter_IO/places answers 400", "(published product 1:7a5d84127c244707dc0e)"} {
		if !strings.Contains(joined, want) {
			t.Errorf("unserved lacks %q:\n%s", want, joined)
		}
	}
	if !strings.Contains(log.String(), "published products without their network pages") {
		t.Fatalf("not logged:\n%s", log)
	}
}

func TestAHiddenProviderIsNotAGap(t *testing.T) {
	t.Parallel()
	s, _ := freshServer(t, newFreshSource(t, store.ProviderRow{Slug: "shifter", Name: "Shifter", Kinds: []string{"residential"},
		FrameworkProvider: sptr("shifter"), Listed: false, Strengths: []string{}, Weaknesses: []string{}, BestFor: []string{}}))
	if rec := get(s.Handler(), "/v1/networks/shifter"); rec.Code != 404 {
		t.Fatalf("hidden network served: %d", rec.Code)
	}
	if hz := health(t, s); hz.Status != "ok" || len(hz.Unserved) != 0 {
		t.Fatalf("healthz status %q unserved %v", hz.Status, hz.Unserved)
	}
}

func TestUnservedPagesAreBounded(t *testing.T) {
	t.Parallel()
	s, _ := freshServer(t, newFreshSource(t))
	v := s.Current()
	pages := *v.Result.Pages
	pages.Published = nil
	for i := range MaxUnserved + 5 {
		pages.Published = append(pages.Published, build.PublishedProduct{ID: fmt.Sprintf("%d:x", i), Provider: "gone", Kind: "residential"})
	}
	res := *v.Result
	res.Pages = &pages
	got := s.unservedPages(&Version{Result: &res})
	if len(got) != MaxUnserved+1 || got[MaxUnserved] != "and 5 more" {
		t.Fatalf("%d lines, last %q", len(got), got[len(got)-1])
	}
}

func TestHealthNamesTheBinarysRevision(t *testing.T) {
	t.Parallel()
	s, _ := freshServer(t, newFreshSource(t))
	rec := get(s.Handler(), "/healthz")
	var raw map[string]map[string]any
	_ = json.Unmarshal(rec.Body.Bytes(), &raw)
	b, ok := raw["binary"]
	if !ok {
		t.Fatalf("healthz has no binary: %s", rec.Body)
	}
	for _, k := range []string{"revision", "time", "modified"} {
		if _, ok := b[k]; !ok {
			t.Errorf("binary lacks %s: %v", k, b)
		}
	}
}
