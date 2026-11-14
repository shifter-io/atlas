package server

import (
	"context"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"testing"
	"time"
)

// newCDNServer is newTestServer with a CDN configured: data answers kept an
// hour at the CDN under tag, and its clock at now.
func newCDNServer(t *testing.T, tag string, now time.Time) *Server {
	t.Helper()
	s := New(&fakeSource{rev: 1}, Options{Interval: 20 * time.Millisecond, SearchLRU: 4, Logger: slog.New(slog.NewTextHandler(io.Discard, nil)),
		FilesDir: t.TempDir(), CDNTTL: time.Hour, CDNTag: tag, Now: func() time.Time { return now }})
	if _, err := s.Refresh(context.Background(), true); err != nil {
		t.Fatal(err)
	}
	return s
}

func TestCDNCacheKeepsAnHourAtTheCDNAndAMinuteInBrowsers(t *testing.T) {
	t.Parallel()
	now := time.Date(2026, 11, 14, 9, 20, 0, 400e6, time.UTC)
	for _, c := range []struct {
		ttl      time.Duration
		tag, cc  string
		wantTag  string
		expires  string
		testName string
	}{
		{time.Hour, "atlas-data", "public, max-age=60, s-maxage=3600", "atlas-data", "Sat, 14 Nov 2026 09:21:00 GMT", "an hour"},
		{90 * time.Minute, "atlas-staging", "public, max-age=60, s-maxage=5400", "atlas-staging", "Sat, 14 Nov 2026 09:21:00 GMT", "own tag"},
		{1500 * time.Millisecond, "", "public, max-age=60, s-maxage=1", DefaultCDNTag, "Sat, 14 Nov 2026 09:21:00 GMT", "whole seconds, default tag"},
	} {
		h := http.Header{}
		cdnCache(h, now, c.ttl, c.tag)
		if h.Get("Cache-Control") != c.cc || h.Get("Expires") != c.expires || h.Get("CDN-Tag") != c.wantTag {
			t.Errorf("%s: Cache-Control %q Expires %q CDN-Tag %q", c.testName, h.Get("Cache-Control"), h.Get("Expires"), h.Get("CDN-Tag"))
		}
	}
}

func TestValidCDN(t *testing.T) {
	t.Parallel()
	for _, c := range []struct {
		ttl time.Duration
		tag string
		ok  bool
	}{
		{0, DefaultCDNTag, true}, {time.Hour, "atlas-data", true}, {time.Second, "a.b_c-1", true}, {time.Hour, "", true},
		{-time.Second, "atlas-data", false}, {500 * time.Millisecond, "atlas-data", false},
		{time.Hour, "two words", false}, {time.Hour, "a,b", false}, {time.Hour, "tag\r\nX-Evil: 1", false}, {time.Hour, strings.Repeat("a", 65), false},
	} {
		if err := ValidCDN(c.ttl, c.tag); (err == nil) != c.ok {
			t.Errorf("ValidCDN(%v, %q) = %v, want ok %v", c.ttl, c.tag, err, c.ok)
		}
	}
}

// TestWithACDNDataAnswersAreKeptAnHourAndTagged: every data answer (pages,
// search, coverage, registered answers, their 304s) gets the CDN policy; the
// other classes keep their own Cache-Control and carry no CDN-Tag.
func TestWithACDNDataAnswersAreKeptAnHourAndTagged(t *testing.T) {
	t.Parallel()
	now := time.Date(2026, 11, 14, 9, 50, 0, 0, time.UTC)
	s := newCDNServer(t, "atlas-data", now)
	s.current.Load().NextData = now.Add(10 * time.Minute) // the CDN TTL does not follow the data schedule
	h := s.Handler()
	const hour = "public, max-age=60, s-maxage=3600"
	for _, p := range []string{"/v1/coverage", "/v1/coverage/alpha-net", "/v1/search?q=alpha", "/v1/networks",
		"/v1/networks/alpha-net", "/v1/networks/alpha-net/places", "/v1/rankings", "/v1/rankings?range=24h", "/v1/countries", "/v1/home", "/v1/meta", "/v1/codes"} {
		r := getOnce(h, p)
		if r.Code != http.StatusOK || r.Header().Get("Cache-Control") != hour || r.Header().Get("Expires") != "Sat, 14 Nov 2026 09:51:00 GMT" ||
			r.Header().Get("CDN-Tag") != "atlas-data" {
			t.Errorf("%s: %d Cache-Control %q Expires %q CDN-Tag %q", p, r.Code, r.Header().Get("Cache-Control"), r.Header().Get("Expires"), r.Header().Get("CDN-Tag"))
		}
	}
	etag := getOnce(h, "/v1/networks").Header().Get("ETag")
	if r := getOnce(h, "/v1/networks", "If-None-Match", etag); r.Code != http.StatusNotModified || r.Header().Get("Cache-Control") != hour ||
		r.Header().Get("CDN-Tag") != "atlas-data" || r.Header().Get("Expires") != "Sat, 14 Nov 2026 09:51:00 GMT" {
		t.Errorf("304: %d %v", r.Code, r.Header())
	}
	otherClassesKeepTheirCache(t, s)
}

// otherClassesKeepTheirCache checks the answers that are not data answers:
// the same Cache-Control as without a CDN, and no CDN-Tag.
func otherClassesKeepTheirCache(t *testing.T, s *Server) {
	t.Helper()
	h := s.Handler()
	v := s.current.Load()
	for p, want := range map[string]string{
		"/v1/version":           CacheVersion,
		"/v1/dataset":           CacheVersion,
		v.DatasetPath:           CacheFile,
		"/v1/events":            CacheEvents,
		"/v1/events/stats":      CacheEvents,
		"/healthz":              CacheNone,
		"/v1/networks/none":     CacheNone,
		"/v1/networks?x=1":      CacheNone,
		"/v1/rankings?range=1y": CacheNone,
		// An older dataset name redirects, uncached, to the current file.
		"/v1/files/dataset-" + strings.Repeat("0", 64) + ".json": CacheNone,
	} {
		r := getOnce(h, p)
		if cc := r.Header().Get("Cache-Control"); cc != want || r.Header().Get("CDN-Tag") != "" {
			t.Errorf("%s: %d Cache-Control %q CDN-Tag %q, want %q and none", p, r.Code, cc, r.Header().Get("CDN-Tag"), want)
		}
	}
}

func TestWithoutACDNNothingIsTaggedAndOtherClassesKeepTheirCache(t *testing.T) {
	t.Parallel()
	s := newTestServer(t, &fakeSource{rev: 1})
	h := s.Handler()
	for _, p := range []string{"/v1/networks", "/v1/coverage", "/v1/home"} {
		if r := getOnce(h, p); r.Header().Get("CDN-Tag") != "" || strings.Contains(r.Header().Get("Cache-Control"), "max-age=60,") {
			t.Errorf("%s: Cache-Control %q CDN-Tag %q", p, r.Header().Get("Cache-Control"), r.Header().Get("CDN-Tag"))
		}
	}
	otherClassesKeepTheirCache(t, s)
}

// TestWithACDNNoParamRoutesRefuseAQuery: behind a CDN that varies by query
// string, a made-up parameter on a route that takes none gets a small,
// uncached 400 instead of a fresh copy of the answer; without a CDN too
// (SEC-PARAMS-1).
func TestWithACDNNoParamRoutesRefuseAQuery(t *testing.T) {
	t.Parallel()
	cdn := newCDNServer(t, "", time.Now())
	off := newTestServer(t, &fakeSource{rev: 1})
	file := cdn.current.Load().DatasetPath
	for _, p := range []string{"/v1/version?zz=1", "/v1/dataset?zz=1", file + "?zz=1", "/v1/coverage?zz=1",
		"/v1/coverage/alpha-net?zz=1", "/v1/version?&", "/v1/version?zz"} {
		r := getOnce(cdn.Handler(), p)
		if r.Code != http.StatusBadRequest || r.Header().Get("Cache-Control") != CacheNone || r.Header().Get("CDN-Tag") != "" ||
			!strings.Contains(r.Body.String(), `"code":"unknown_parameter"`) {
			t.Errorf("with a CDN %s: %d %q", p, r.Code, r.Header().Get("Cache-Control"))
		}
		offFile := strings.Replace(p, file, off.current.Load().DatasetPath, 1)
		if r := getOnce(off.Handler(), offFile); r.Code != http.StatusBadRequest || !strings.Contains(r.Body.String(), `"code":"unknown_parameter"`) {
			t.Errorf("without a CDN %s: %d", offFile, r.Code)
		}
	}
	for p, code := range map[string]int{"/v1/version": 200, "/v1/dataset": 302, file: 200, "/v1/coverage/alpha-net": 200,
		"/v1/rankings?range=24h": 200, "/v1/search?q=alpha": 200, "/v1/events?limit=5": 200, "/v1/networks/alpha-net/places?page=2": 200} {
		if r := getOnce(cdn.Handler(), p); r.Code != code {
			t.Errorf("with a CDN %s: %d, want %d", p, r.Code, code)
		}
	}
}
