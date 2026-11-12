package server

import (
	"context"
	"net/http"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/build"
	"github.com/Zeus-Development/proxy-index/api/internal/store"
)

func TestChangeSplitsDataFromEvents(t *testing.T) {
	t.Parallel()
	id := func(n int64) *int64 { return &n }
	served := &Version{Result: &build.Result{Fingerprint: store.Fingerprint{SiteRevision: 1, MaxEventID: id(7)}}}
	for _, c := range []struct {
		name  string
		cur   *Version
		fp    store.Fingerprint
		force bool
		want  changeKind
	}{
		{"nothing served", nil, store.Fingerprint{SiteRevision: 1, MaxEventID: id(7)}, false, changeData},
		{"unchanged", served, store.Fingerprint{SiteRevision: 1, MaxEventID: id(7)}, false, changeNone},
		{"forced", served, store.Fingerprint{SiteRevision: 1, MaxEventID: id(7)}, true, changeData},
		{"new event", served, store.Fingerprint{SiteRevision: 1, MaxEventID: id(8)}, false, changeEvents},
		{"events emptied", served, store.Fingerprint{SiteRevision: 1}, false, changeEvents},
		{"site edit", served, store.Fingerprint{SiteRevision: 2, MaxEventID: id(7)}, false, changeData},
		{"site edit and new event", served, store.Fingerprint{SiteRevision: 2, MaxEventID: id(8)}, false, changeData},
		{"new build", served, store.Fingerprint{SiteRevision: 1, MaxEventID: id(7), BuildID: id(3)}, false, changeData},
	} {
		if got := change(c.cur, c.fp, c.force); got != c.want {
			t.Errorf("%s: change %d, want %d", c.name, got, c.want)
		}
	}
	if k := (store.Fingerprint{MaxEventID: id(12)}).EventsKey(); k != "12" {
		t.Errorf("events key %q", k)
	}
}

// A full build keeps the previous body, pointer and compressed variants,
// wherever the new bytes are the same, and renders only what changed.
// /v1/version (LevelFast) is always rendered again.
func TestRenderReusesBodiesWhoseBytesDidNotChange(t *testing.T) {
	t.Parallel()
	s := newTestServer(t, &fakeSource{rev: 1, events: events(5), complete: true})
	prev := s.Current()
	same, err := renderVersion(context.Background(), 2, prev.Result, s.opts, prev)
	if err != nil {
		t.Fatal(err)
	}
	for path, r := range prev.Static {
		if reused := same.Static[path] == r; reused != (path != versionPath) {
			t.Errorf("%s reused %v", path, reused)
		}
	}
	if same.Dataset != nil {
		t.Error("renderVersion wrote a dataset file: the refresh does (datasetfile.go)")
	}
	changed := *prev.Result
	ds := *changed.Dataset
	ds.GeneratedAt = "2026-10-02T00:00:00.000Z"
	changed.Dataset = &ds
	next, err := renderVersion(context.Background(), 3, &changed, s.opts, prev)
	if err != nil {
		t.Fatal(err)
	}
	if next.Static["/v1/search-index"] != prev.Static["/v1/search-index"] {
		t.Error("the unchanged search index was compressed again")
	}
	fresh, err := RenderVersion(4, prev.Result, s.opts)
	if err != nil || fresh.Dataset == prev.Dataset || fresh.Dataset.SHA256 != prev.Dataset.SHA256 {
		t.Fatalf("the same data gives the same dataset file: %v", err)
	}
}

// The reuse rule: same identity bytes (by SHA-256, then compared), same
// Cache-Control and same status.
func TestReuseNeedsTheSameBytesPolicyAndStatus(t *testing.T) {
	t.Parallel()
	old, err := Render(http.StatusOK, map[string]int{"a": 1}, CacheData, LevelBest)
	if err != nil {
		t.Fatal(err)
	}
	index := bodies{bodyKey{old.ETag, old.CacheControl, old.Status}: old}
	check := func(name string, b bodies, status int, value any, cache string, want bool) {
		t.Helper()
		r, err := b.render(status, value, cache)
		if err != nil {
			t.Fatal(err)
		}
		if (r == old) != want {
			t.Errorf("%s: reused %v, want %v", name, r == old, want)
		}
		if r != old && (r.Status != status || r.CacheControl != cache || len(r.Brotli) == 0) {
			t.Errorf("%s: rendered %d %q", name, r.Status, r.CacheControl)
		}
	}
	check("same bytes", index, http.StatusOK, map[string]int{"a": 1}, CacheData, true)
	check("other bytes", index, http.StatusOK, map[string]int{"a": 2}, CacheData, false)
	check("other policy", index, http.StatusOK, map[string]int{"a": 1}, CacheEvents, false)
	check("other status", index, http.StatusNotFound, map[string]int{"a": 1}, CacheData, false)
	forged := bodies{bodyKey{old.ETag, old.CacheControl, old.Status}: {Status: http.StatusOK, Identity: []byte(`{"a":9}`), CacheControl: CacheData}}
	if r, _ := forged.render(http.StatusOK, map[string]int{"a": 1}, CacheData); string(r.Identity) != `{"a":1}` {
		t.Error("a body was reused on its hash alone")
	}
	if len(reusable(nil)) != 0 {
		t.Error("nil previous version indexed bodies")
	}
	if _, err := index.render(http.StatusOK, func() {}, CacheData); err == nil {
		t.Error("an unencodable value rendered")
	}
}
