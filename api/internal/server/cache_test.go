package server

import (
	"context"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/Zeus-Development/proxy-index-website/api/internal/build"
	"github.com/Zeus-Development/proxy-index-website/api/internal/model"
	"github.com/Zeus-Development/proxy-index-website/api/internal/store"
)

func TestNextDataIsTheEndOfTheSnapshotsPeriod(t *testing.T) {
	at := func(s string) time.Time {
		v, err := time.Parse(time.RFC3339, s)
		if err != nil {
			t.Fatal(err)
		}
		return v
	}
	for _, c := range []struct {
		asOf   model.Str
		period time.Duration
		want   time.Time
	}{
		{model.S("2026-11-03T13:00:00Z"), time.Hour, at("2026-11-03T14:00:00Z")},
		{model.S("2026-11-03T13:05:00+00:00"), time.Hour, at("2026-11-03T14:00:00Z")},
		{model.S("2026-11-03T15:05:00+02:00"), time.Hour, at("2026-11-03T14:00:00Z")},
		{model.S("2026-11-03T13:05:00Z"), 5 * time.Minute, at("2026-11-03T13:10:00Z")},
		{nil, time.Hour, time.Time{}},
		{model.S("not a time"), time.Hour, time.Time{}},
		{model.S("2026-11-03T13:00:00Z"), 0, time.Time{}},
	} {
		if got := nextData(c.asOf, c.period); !got.Equal(c.want) {
			t.Errorf("nextData(%v, %v) = %v, want %v", c.asOf, c.period, got, c.want)
		}
	}
}

func TestDataCacheEndsAtTheNextUpdate(t *testing.T) {
	now := time.Date(2026, 11, 3, 13, 20, 0, 400e6, time.UTC)
	next := time.Date(2026, 11, 3, 14, 0, 0, 0, time.UTC)
	for _, c := range []struct {
		name    string
		next    time.Time
		cc      string
		expires string
	}{
		// 39m59.6s left: rounded down, so the copy expires before the update is due.
		{"before the update", next, "public, max-age=2399, s-maxage=2399", "Tue, 03 Nov 2026 13:59:59 GMT"},
		{"update due now", now, "public, max-age=30, s-maxage=30", "Tue, 03 Nov 2026 13:20:30 GMT"},
		{"update late", next.Add(-time.Hour), "public, max-age=30, s-maxage=30", "Tue, 03 Nov 2026 13:20:30 GMT"},
		{"no snapshot", time.Time{}, "public, max-age=30, s-maxage=30", "Tue, 03 Nov 2026 13:20:30 GMT"},
		{"under a second left", now.Add(500 * time.Millisecond), "public, max-age=1, s-maxage=1", "Tue, 03 Nov 2026 13:20:01 GMT"},
	} {
		h := http.Header{}
		dataCache(h, now, c.next, DefaultCacheFloor)
		if h.Get("Cache-Control") != c.cc || h.Get("Expires") != c.expires {
			t.Errorf("%s: Cache-Control %q Expires %q, want %q %q", c.name, h.Get("Cache-Control"), h.Get("Expires"), c.cc, c.expires)
		}
	}
}

func TestRenderVersionTakesTheNextUpdateFromTheSnapshot(t *testing.T) {
	res, err := build.Build(fixture(1, nil, true))
	if err != nil {
		t.Fatal(err)
	}
	v, err := RenderVersion(1, res, Options{DataPeriod: time.Hour})
	if err != nil {
		t.Fatal(err)
	}
	if !v.NextData.IsZero() {
		t.Fatalf("no snapshot, NextData %v", v.NextData)
	}
	res.Dataset.Snapshot.AsOf = model.S("2026-11-03T13:00:00Z")
	if v, err = RenderVersion(2, res, Options{DataPeriod: time.Hour}); err != nil {
		t.Fatal(err)
	}
	if want := time.Date(2026, 11, 3, 14, 0, 0, 0, time.UTC); !v.NextData.Equal(want) {
		t.Fatalf("NextData %v, want %v", v.NextData, want)
	}
}

// TestDataAnswersAreCachedUntilTheNextUpdate: every data answer (static,
// page and search, 304s included) carries the TTL to the next update; the
// version and the dataset redirect stay short, and the dataset file, the live
// tape, health and errors keep their own.
func TestDataAnswersAreCachedUntilTheNextUpdate(t *testing.T) {
	s := newTestServer(t, &fakeSource{rev: 1})
	now := time.Date(2026, 11, 3, 13, 50, 0, 0, time.UTC)
	s.opts.Now = func() time.Time { return now }
	v := s.current.Load()
	v.NextData = now.Add(10 * time.Minute)
	h := s.Handler()
	const ten = "public, max-age=600, s-maxage=600"
	for _, p := range []string{"/v1/coverage", "/v1/search-index", "/v1/networks", "/v1/rankings", "/v1/countries", "/v1/search?q=alpha"} {
		r := getOnce(h, p)
		if cc := r.Header().Get("Cache-Control"); cc != ten || r.Header().Get("Expires") != "Tue, 03 Nov 2026 14:00:00 GMT" {
			t.Errorf("%s: %d Cache-Control %q Expires %q", p, r.Code, cc, r.Header().Get("Expires"))
		}
	}
	etag := getOnce(h, "/v1/coverage").Header().Get("ETag")
	if r := getOnce(h, "/v1/coverage", "If-None-Match", etag); r.Code != http.StatusNotModified || r.Header().Get("Cache-Control") != ten {
		t.Errorf("304: %d %q", r.Code, r.Header().Get("Cache-Control"))
	}
	for p, want := range map[string]string{
		"/v1/version":       CacheVersion,
		"/v1/dataset":       CacheVersion,
		v.DatasetPath:       CacheFile,
		"/v1/events":        CacheEvents,
		"/healthz":          CacheNone,
		"/v1/networks/none": CacheNone,
	} {
		if cc := getOnce(h, p).Header().Get("Cache-Control"); cc != want {
			t.Errorf("%s: Cache-Control %q, want %q", p, cc, want)
		}
	}
	v.NextData = now.Add(-time.Minute) // the update is late: short TTL until it appears
	if cc := getOnce(h, "/v1/networks").Header().Get("Cache-Control"); cc != "public, max-age=30, s-maxage=30" {
		t.Errorf("overdue: %q", cc)
	}
}

// TestANewEventKeepsTheDatasetFile: even a rebuild caused only by a new test
// event (same snapshot, same next update) writes no new dataset file, so the
// revision stays (contract K4); a data change writes one, and /v1/version,
// which open pages poll, stays short-lived even while data answers are cached
// until the next update.
func TestANewEventKeepsTheDatasetFile(t *testing.T) {
	src := &fakeSource{rev: 1, complete: true}
	s := newTestServer(t, src)
	h := s.Handler()
	next := time.Now().Add(time.Hour)
	s.current.Load().NextData = next
	before := Revision(s.current.Load())
	src.mu.Lock()
	src.events = []store.Event{event(1, time.Date(2026, 9, 26, 14, 50, 0, 0, time.UTC), "pulse_down", "acme")}
	src.mu.Unlock()
	// The fake's fingerprint ignores events, so force the rebuild the real
	// fingerprint (max event id) would trigger.
	if _, err := s.Refresh(context.Background(), true); err != nil {
		t.Fatal(err)
	}
	v := s.current.Load()
	if Revision(v) != before || v.Dataset != s.files.lookup(v.Dataset.Name(), time.Now()) {
		t.Fatal("a new event wrote a new dataset file")
	}
	src.set(2, nil)
	if _, err := s.Refresh(context.Background(), false); err != nil {
		t.Fatal(err)
	}
	if v = s.current.Load(); Revision(v) == before {
		t.Fatal("a data change kept the dataset file")
	}
	if !v.NextData.Equal(nextData(v.Result.Dataset.Snapshot.AsOf, s.opts.DataPeriod)) {
		t.Fatalf("NextData %v changed without a snapshot", v.NextData)
	}
	r := getOnce(h, "/v1/version")
	if cc := r.Header().Get("Cache-Control"); cc != CacheVersion || !strings.Contains(r.Body.String(), Revision(v)) {
		t.Fatalf("version %q %s", cc, r.Body.String())
	}
}

func TestDataPeriodMustDivideADay(t *testing.T) {
	for _, ok := range []time.Duration{time.Hour, 5 * time.Minute, 30 * time.Second, 24 * time.Hour} {
		if err := ValidDataPeriod(ok); err != nil {
			t.Errorf("%v: %v", ok, err)
		}
	}
	for _, bad := range []time.Duration{0, -time.Hour, 7 * time.Minute, 5 * time.Hour, 48 * time.Hour} {
		if err := ValidDataPeriod(bad); err == nil {
			t.Errorf("%v accepted", bad)
		}
	}
}
