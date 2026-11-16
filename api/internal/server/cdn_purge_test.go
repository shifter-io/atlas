package server

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/Zeus-Development/atlas/api/internal/store"
)

// testCDNKey is the fake CDN's key; no log, error or /healthz may hold it.
const testCDNKey = "k3y-NEVER-SHOWN-0123456789"

// fakeCDN is a purge endpoint: it records each request and answers status.
type fakeCDN struct {
	*httptest.Server
	mu       sync.Mutex
	status   int
	requests []purgeRequest
	// during, when set, runs inside each request, before the answer.
	during func()
}

type purgeRequest struct {
	method, key, contentType, tag string
}

func newFakeCDN(t *testing.T, status int) *fakeCDN {
	t.Helper()
	c := &fakeCDN{status: status}
	c.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body struct{ CacheTag string }
		json.NewDecoder(r.Body).Decode(&body) //nolint:errcheck // an empty tag fails the test
		c.mu.Lock()
		c.requests = append(c.requests, purgeRequest{r.Method, r.Header.Get("AccessKey"), r.Header.Get("Content-Type"), body.CacheTag})
		status, during := c.status, c.during
		c.mu.Unlock()
		if during != nil {
			during()
		}
		w.WriteHeader(status)
	}))
	t.Cleanup(c.Close)
	return c
}

func (c *fakeCDN) got() []purgeRequest {
	c.mu.Lock()
	defer c.mu.Unlock()
	return append([]purgeRequest{}, c.requests...)
}

func (c *fakeCDN) set(status int, during func()) {
	c.mu.Lock()
	c.status, c.during = status, during
	c.mu.Unlock()
}

// writeKey writes key to a private key file and returns its path.
func writeKey(t *testing.T, key string, mode os.FileMode) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "cdn-key")
	if err := os.WriteFile(path, []byte(key+"\n"), mode); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(path, mode); err != nil {
		t.Fatal(err)
	}
	return path
}

// newPurgeServer serves src with a CDN purged at cdn under "atlas-data", its
// log in logs, and builds once (no purge: nothing served before).
func newPurgeServer(t *testing.T, src Source, cdn *fakeCDN, logs io.Writer, window, settle time.Duration) (*Server, *Purger) {
	t.Helper()
	p, err := NewPurger(cdn.URL+"/pullzone/1/purgeCache", writeKey(t, testCDNKey, 0o600), "atlas-data")
	if err != nil {
		t.Fatal(err)
	}
	p.window, p.settle = window, settle
	s := New(src, Options{Interval: time.Hour, SearchLRU: 4, Logger: slog.New(slog.NewTextHandler(logs, nil)), FilesDir: t.TempDir(),
		CDNTTL: time.Hour, Purger: p})
	if _, err := s.Refresh(context.Background(), true); err != nil {
		t.Fatal(err)
	}
	if n := len(cdn.got()); n != 0 {
		t.Fatalf("the first build purged the CDN %d times, want none", n)
	}
	return s, p
}

// wait is a Purger's wait that never sleeps: it moves c on by d and is
// recorded.
func (c *fakeClock) wait(ctx context.Context, d time.Duration) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.now = c.now.Add(d)
	c.waits = append(c.waits, d)
	return ctx.Err() == nil
}

func (c *fakeClock) waited() []time.Duration {
	c.mu.Lock()
	defer c.mu.Unlock()
	return slices.Clone(c.waits)
}

// announcedRevision is /v1/version's revision and /v1/dataset's Location
// ("" when /v1/version is not JSON).
func announcedRevision(h http.Handler) (string, string) {
	var v VersionInfo
	json.Unmarshal(getOnce(h, versionPath).Body.Bytes(), &v) //nolint:errcheck // an empty revision fails the test
	return v.Revision, getOnce(h, "/v1/dataset").Header().Get("Location")
}

func adminCDN(t *testing.T, s *Server) (string, map[string]any, string) {
	t.Helper()
	body := get(s.AdminHandler(), "/healthz").Body.String()
	var hz struct {
		Status string         `json:"status"`
		CDN    map[string]any `json:"cdn"`
	}
	if err := json.Unmarshal([]byte(body), &hz); err != nil {
		t.Fatal(err)
	}
	return hz.Status, hz.CDN, body
}

// TestAPurgeComesBeforeTheNewVersionIsAnnounced: on a full rebuild the data
// answers switch at once, while /v1/version and the /v1/dataset redirect keep
// naming the previous build until the CDN confirmed the purge, plus the
// settle time.
func TestAPurgeComesBeforeTheNewVersionIsAnnounced(t *testing.T) {
	t.Parallel()
	src := &fakeSource{rev: 1}
	cdn := newFakeCDN(t, http.StatusOK)
	const settle = 150 * time.Millisecond
	s, _ := newPurgeServer(t, src, cdn, io.Discard, time.Minute, settle)
	h := s.Handler()
	oldRev, oldLoc := announcedRevision(h)
	during := make(chan [3]string, 1)
	var answered atomic.Int64
	cdn.set(http.StatusOK, func() {
		rev, loc := announcedRevision(h)
		during <- [3]string{rev, loc, get(h, "/v1/networks/alpha-net").Body.String()}
		answered.Store(time.Now().UnixNano())
	})
	src.set(2, nil)
	if changed, err := s.Refresh(context.Background(), false); !changed || err != nil {
		t.Fatalf("Refresh = %v, %v", changed, err)
	}
	announcedAt := time.Now()
	newRev, newLoc := announcedRevision(h)
	if newRev == oldRev || newLoc == oldLoc || newLoc != strings.TrimPrefix(s.Current().DatasetPath, "/v1/") {
		t.Fatalf("after the purge /v1/version %s and /v1/dataset %s, want the new build's (before %s, %s)", newRev, newLoc, oldRev, oldLoc)
	}
	seen := <-during
	duringRev, duringLoc, duringNet := seen[0], seen[1], seen[2]
	if duringRev != oldRev || duringLoc != oldLoc {
		t.Errorf("during the purge /v1/version %s and /v1/dataset %s, want the previous build's %s, %s", duringRev, duringLoc, oldRev, oldLoc)
	}
	if !strings.Contains(duringNet, "Alpha r2") {
		t.Errorf("during the purge a data answer was not the new build's: %.200s", duringNet)
	}
	if wait := announcedAt.Sub(time.Unix(0, answered.Load())); wait < settle {
		t.Errorf("announced %v after the CDN confirmed, want at least %v", wait, settle)
	}
	want := []purgeRequest{{http.MethodPost, testCDNKey, "application/json", "atlas-data"}}
	if got := cdn.got(); len(got) != 1 || got[0] != want[0] {
		t.Errorf("purge requests %+v, want %+v", got, want)
	}
	status, c, _ := adminCDN(t, s)
	if status != "ok" || c["lastPurgeAt"] == nil || c["lastError"] != nil {
		t.Errorf("/healthz status %s cdn %v, want ok with a purge time and no error", status, c)
	}
}

// TestAFailedPurgeAnnouncesAnywayAndIsRecorded: the CDN refusing every try
// costs 3 tries within the window, then the new build is announced, one log
// line says so and /healthz cdn holds the error until a purge succeeds. The
// key is in no log line and not in /healthz.
func TestAFailedPurgeAnnouncesAnywayAndIsRecorded(t *testing.T) {
	t.Parallel()
	src := &fakeSource{rev: 1}
	cdn := newFakeCDN(t, http.StatusUnauthorized)
	var logs syncBuffer
	// The tries' schedule on a fake clock, not the runner's: each try may
	// still take window/3 = 1 minute of real time, ample on any runner (500 ms
	// was not under -race), and the test waits for none. A settle after a
	// failure would be a wait of an hour.
	s, p := newPurgeServer(t, src, cdn, &logs, 3*time.Minute, time.Hour)
	clock := &fakeClock{}
	p.now, p.wait = clock.Now, clock.wait
	h := s.Handler()
	oldRev, _ := announcedRevision(h)
	src.set(2, nil)
	if changed, err := s.Refresh(context.Background(), false); !changed || err != nil {
		t.Fatalf("Refresh = %v, %v", changed, err)
	}
	if waits := clock.waited(); fmt.Sprint(waits) != fmt.Sprint([]time.Duration{time.Minute, time.Minute}) {
		t.Errorf("3 tries over a 3 minute window waited %v, want the second to start at 1 minute, the third at 2 and no settle", waits)
	}
	if rev, _ := announcedRevision(h); rev == oldRev || rev != Revision(s.Current()) {
		t.Errorf("after a failed purge /v1/version is %s, want the new build's %s", rev, Revision(s.Current()))
	}
	if n := len(cdn.got()); n != purgeTries {
		t.Errorf("%d purge requests, want %d", n, purgeTries)
	}
	if n := strings.Count(logs.String(), "CDN purge failed"); n != 1 {
		t.Errorf("%d failure log lines, want 1:\n%s", n, logs.String())
	}
	status, c, body := adminCDN(t, s)
	if status != "degraded" || c["lastError"] != "CDN purge: status 401" || c["lastPurgeAt"] == nil {
		t.Errorf("/healthz status %s cdn %v, want degraded with the error", status, c)
	}
	for name, text := range map[string]string{"log": logs.String(), "/healthz": body} {
		if strings.Contains(text, testCDNKey) {
			t.Errorf("the CDN key is in the %s", name)
		}
	}
	// The next purge succeeds: the error clears.
	p.window, p.settle = time.Minute, time.Millisecond
	cdn.set(http.StatusNoContent, nil)
	src.set(3, nil)
	if _, err := s.Refresh(context.Background(), false); err != nil {
		t.Fatal(err)
	}
	if status, c, _ := adminCDN(t, s); status != "ok" || c["lastError"] != nil {
		t.Errorf("after a good purge /healthz status %s cdn %v", status, c)
	}
	if strings.Contains(logs.String(), testCDNKey) {
		t.Error("the CDN key is in the log")
	}
}

// eventsSource is a fakeSource whose fingerprint also names the newest
// event, so a new event alone is an events-only refresh.
type eventsSource struct {
	*fakeSource
	maxEvent atomic.Int64
}

func (e *eventsSource) Fingerprint(ctx context.Context) (store.Fingerprint, error) {
	fp, err := e.fakeSource.Fingerprint(ctx)
	id := e.maxEvent.Load()
	fp.MaxEventID = &id
	return fp, err
}

// TestOnlyAFullRebuildPurges: an events-only refresh neither purges nor holds
// /v1/version; without a Purger /healthz has no cdn.
func TestOnlyAFullRebuildPurges(t *testing.T) {
	t.Parallel()
	src := &eventsSource{fakeSource: &fakeSource{rev: 1}}
	cdn := newFakeCDN(t, http.StatusOK)
	s, _ := newPurgeServer(t, src, cdn, io.Discard, time.Minute, time.Millisecond)
	src.maxEvent.Store(9)
	if changed, err := s.Refresh(context.Background(), false); !changed || err != nil {
		t.Fatalf("events refresh = %v, %v", changed, err)
	}
	if n := len(cdn.got()); n != 0 {
		t.Errorf("an events-only refresh purged %d times", n)
	}
	if _, c, _ := adminCDN(t, s); c == nil || c["lastPurgeAt"] != nil {
		t.Errorf("/healthz cdn %v, want present with no purge yet", c)
	}
	if _, c, _ := adminCDN(t, newTestServer(t, &fakeSource{rev: 1})); c != nil {
		t.Errorf("without a Purger /healthz has cdn %v", c)
	}
}

func TestNewPurgerRefusesUnsafeSettingsWithoutShowingTheKey(t *testing.T) {
	t.Parallel()
	good := writeKey(t, testCDNKey, 0o600)
	for _, c := range []struct {
		name, url, keyFile, tag string
	}{
		{"open key file", "https://cdn.example/purge", writeKey(t, testCDNKey, 0o644), ""},
		{"key with a space", "https://cdn.example/purge", writeKey(t, "two words", 0o600), ""},
		{"empty key", "https://cdn.example/purge", writeKey(t, "", 0o600), ""},
		{"key too long", "https://cdn.example/purge", writeKey(t, strings.Repeat("k", 1025), 0o600), ""},
		{"missing key file", "https://cdn.example/purge", filepath.Join(t.TempDir(), "none"), ""},
		{"plain http to a remote host", "http://cdn.example/purge", good, ""},
		{"no host", "https:///purge", good, ""},
		{"bad tag", "https://cdn.example/purge", good, "a b"},
	} {
		_, err := NewPurger(c.url, c.keyFile, c.tag)
		if err == nil {
			t.Errorf("%s: accepted", c.name)
		} else if strings.Contains(err.Error(), testCDNKey) {
			t.Errorf("%s: the error shows the key", c.name)
		}
	}
	p, err := NewPurger("http://127.0.0.1:1/purge", good, "")
	if err != nil || p.tag != DefaultCDNTag || p.key != testCDNKey {
		t.Fatalf("loopback http: %v", err)
	}
}

// TestAPurgeFollowsNoRedirect: a redirect is a failed purge, so the key
// never reaches another address.
func TestAPurgeFollowsNoRedirect(t *testing.T) {
	t.Parallel()
	elsewhere := newFakeCDN(t, http.StatusOK)
	redirect := httptest.NewServer(http.RedirectHandler(elsewhere.URL, http.StatusTemporaryRedirect))
	t.Cleanup(redirect.Close)
	p, err := NewPurger(redirect.URL, writeKey(t, testCDNKey, 0o600), "")
	if err != nil {
		t.Fatal(err)
	}
	if err := p.Purge(context.Background()); err == nil || len(elsewhere.got()) != 0 {
		t.Errorf("Purge = %v with %d requests elsewhere, want a failure and none", err, len(elsewhere.got()))
	}
}

// syncBuffer is a bytes.Buffer safe for a logger and the test together.
type syncBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *syncBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *syncBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}
