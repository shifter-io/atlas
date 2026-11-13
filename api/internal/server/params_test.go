package server

import (
	"bytes"
	"context"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

// lockedBuffer is a log sink safe for concurrent handlers.
type lockedBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *lockedBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *lockedBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

// A control character in an events filter that would be read from the
// database (an incomplete window, a before cursor that exists) answers 400
// before the database is asked, with no error logged (it answered 503 and
// logged the database's refusal).
func TestEventsRefuseControlTextBeforeTheDatabase(t *testing.T) {
	t.Parallel()
	src := &fakeSource{rev: 1, events: events(30), complete: false}
	log := &lockedBuffer{}
	s := New(src, Options{Interval: time.Hour, Logger: slog.New(slog.NewTextHandler(log, nil)), FilesDir: t.TempDir()})
	if _, err := s.Refresh(context.Background(), true); err != nil {
		t.Fatal(err)
	}
	if readTape(t, s, "/v1/events?before=5&target=generic_http"); src.dbQueries.Load() != 1 {
		t.Fatalf("the incomplete window did not ask the database: %d queries", src.dbQueries.Load())
	}
	for _, q := range []string{"before=5&target=generic%01http", "before=5&target=%00", "before=5&q=a%7F", "before=5&target=%ff", "before=5&tar%00get=x"} {
		r := get(s.Handler(), "/v1/events?"+q)
		if r.Code != 400 || !strings.Contains(r.Body.String(), `"format":"text"`) || r.Header().Get("Cache-Control") != CacheNone {
			t.Errorf("%s: %d %s %v", q, r.Code, r.Body.String(), r.Header())
		}
	}
	if n := src.dbQueries.Load(); n != 1 {
		t.Errorf("bad text asked the database: %d queries, want 1", n)
	}
	if strings.Contains(log.String(), "level=ERROR") {
		t.Errorf("logged an error: %s", log.String())
	}
}

func TestValidText(t *testing.T) {
	t.Parallel()
	for s, want := range map[string]bool{"": true, "net00": true, "a b,c%_'\"": true, "Zürich": true, "‮": true,
		"a\x00": false, "\t": false, "\x1f": false, "\x7f": false, "\xff": false, "\xc3\x28": false} {
		if got := validText(s); got != want {
			t.Errorf("validText(%q) = %v, want %v", s, got, want)
		}
	}
}

// The routes that took any parameter refuse every one, uncached; a path
// that is not clean answers 404 instead of the mux's cacheable redirect, and
// a path or a query that is not text answers 400.
func TestEveryRouteRefusesParametersAndBadText(t *testing.T) {
	t.Parallel()
	s := newTestServer(t, &fakeSource{rev: 1, events: events(3), complete: true})
	h := s.Handler()
	file := s.Current().Dataset.Path()
	for _, p := range []string{"/v1/dataset", file, "/v1/version", "/v1/search-index", "/v1/coverage", "/v1/coverage/alpha-net", "/healthz",
		"/v1/networks", "/v1/countries", "/v1/search", "/v1/events"} {
		r := getOnce(h, p+"?cb=1")
		if r.Code != 400 || !strings.Contains(r.Body.String(), `"unknown_parameter"`) || r.Header().Get("Cache-Control") != CacheNone {
			t.Errorf("%s?cb=1: %d %s", p, r.Code, r.Body.String())
		}
		if r := getOnce(h, p); r.Code == 400 {
			t.Errorf("%s: %d %s", p, r.Code, r.Body.String())
		}
	}
	cases := map[string]int{
		"/v1/networks/alpha-net/../beta-proxy": 404, "/v1/networks//alpha-net": 404, "/v1/coverage/%2e%2e": 404,
		"/v1/networks/": 404, "/v1/coverage/a%00": 400, "/v1/networks/%ff": 400, "/v1/search?q=a;b": 400,
		"/v1/search?q=%zz": 400, "/v1/search?q=%e2%80%ae": 200,
	}
	for p, want := range cases {
		if r := getOnce(h, p); r.Code != want {
			t.Errorf("%s: %d, want %d: %s", p, r.Code, want, r.Body.String())
		}
	}
}

// Every method but GET and HEAD answers 405 with Allow, probe paths 404,
// and every answer carries nosniff and no credentials header.
func TestHTTPHardening(t *testing.T) {
	t.Parallel()
	s := newTestServer(t, &fakeSource{rev: 1, events: events(3), complete: true})
	h := s.Handler()
	for _, m := range []string{"POST", "PUT", "DELETE", "PATCH", "OPTIONS", "TRACE"} {
		for _, p := range []string{"/v1/networks", "/v1/dataset", "/nowhere"} {
			rec := httptest.NewRecorder()
			h.ServeHTTP(rec, httptest.NewRequest(m, p, nil))
			if rec.Code != 405 || rec.Header().Get("Allow") != "GET, HEAD" {
				t.Errorf("%s %s: %d Allow %q", m, p, rec.Code, rec.Header().Get("Allow"))
			}
		}
	}
	paths := []string{"/.env", "/.git/config", "/debug/pprof/", "/v1/networks", "/v1/dataset", s.Current().Dataset.Path(),
		"/v1/version", "/healthz", "/v1/networks?x=1", "/v1/nowhere", "/v1/events", "/v1/networks/alpha-net/../x"}
	for _, p := range paths {
		r := getOnce(h, p)
		if strings.HasPrefix(p, "/.") || strings.HasPrefix(p, "/debug") {
			if r.Code != 404 {
				t.Errorf("%s: %d, want 404", p, r.Code)
			}
		}
		checkHardHeaders(t, p, r.Result())
		if etag := r.Header().Get("ETag"); etag != "" {
			checkHardHeaders(t, p+" (304)", getOnce(h, p, "If-None-Match", etag).Result())
		}
	}
}

func checkHardHeaders(t *testing.T, p string, r *http.Response) {
	t.Helper()
	io.Copy(io.Discard, r.Body) //nolint:errcheck
	if r.Header.Get("X-Content-Type-Options") != "nosniff" || r.Header.Get("Access-Control-Allow-Credentials") != "" {
		t.Errorf("%s (%d): headers %v", p, r.StatusCode, r.Header)
	}
}
