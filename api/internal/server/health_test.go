package server

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"maps"
	"net/http"
	"slices"
	"strings"
	"testing"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/Zeus-Development/proxy-index/api/internal/store"
)

// publicKeys are the only fields of the public /healthz.
var publicKeys = []string{"builtAt", "reason", "revision", "status", "version"}

// healthKeys decodes a /healthz body and returns its sorted keys.
func healthKeys(t *testing.T, body []byte) (map[string]any, []string) {
	t.Helper()
	var hz map[string]any
	if err := json.Unmarshal(body, &hz); err != nil {
		t.Fatalf("/healthz %s: %v", body, err)
	}
	return hz, slices.Sorted(maps.Keys(hz))
}

// The public /healthz says only whether the API serves: status, version,
// builtAt, revision and reason, uncached and nosniff; the admin handler
// serves the full report and nothing else. A query parameter is refused.
func TestPublicHealthHasOnlyItsFiveFields(t *testing.T) {
	t.Parallel()
	s := newTestServer(t, &fakeSource{rev: 1})
	s.state.lastError = "a build error"
	rec := get(s.Handler(), "/healthz")
	hz, keys := healthKeys(t, rec.Body.Bytes())
	if rec.Code != http.StatusOK || !slices.Equal(keys, publicKeys) || hz["status"] != "degraded" || hz["version"] == 0.0 ||
		hz["builtAt"] == nil || hz["reason"] != nil {
		t.Fatalf("public: %d %s", rec.Code, rec.Body)
	}
	if h := rec.Header(); h.Get("Cache-Control") != CacheNone || h.Get("X-Content-Type-Options") != "nosniff" {
		t.Errorf("public headers %v", h)
	}
	if rev := BinaryInfo().Revision; rev != nil && hz["revision"] != *rev {
		t.Errorf("revision %v, want %s", hz["revision"], *rev)
	}
	admin := get(s.AdminHandler(), "/healthz")
	full, _ := healthKeys(t, admin.Body.Bytes())
	if admin.Code != http.StatusOK || full["lastError"] != "a build error" || full["fingerprint"] == nil || full["binary"] == nil {
		t.Errorf("admin: %d %s", admin.Code, admin.Body)
	}
	if h := admin.Header(); h.Get("Cache-Control") != CacheNone || h.Get("X-Content-Type-Options") != "nosniff" {
		t.Errorf("admin headers %v", h)
	}
	for _, p := range []string{"/v1/networks", "/v1/version", "/debug/pprof/"} {
		if r := get(s.AdminHandler(), p); r.Code != http.StatusNotFound {
			t.Errorf("admin %s: %d", p, r.Code)
		}
	}
	wantError(t, "public ?verbose", get(s.Handler(), "/healthz?verbose=1"), http.StatusBadRequest,
		`{"code":"unknown_parameter","params":{"name":"verbose"}}`)
	wantError(t, "admin ?x", get(s.AdminHandler(), "/healthz?x="), http.StatusBadRequest,
		`{"code":"unknown_parameter","params":{"name":"x"}}`)
}

// Starting and unavailable keep their 503 and status; unavailable gives its
// reason code.
func TestPublicHealthWhileNothingServes(t *testing.T) {
	t.Parallel()
	s := New(&fakeSource{}, Options{})
	rec := get(s.Handler(), "/healthz")
	hz, keys := healthKeys(t, rec.Body.Bytes())
	if rec.Code != http.StatusServiceUnavailable || !slices.Equal(keys, publicKeys) || hz["status"] != "starting" || hz["reason"] != nil {
		t.Errorf("starting: %d %s", rec.Code, rec.Body)
	}
	s.state.reason = BeforeV4Reason
	rec = get(s.Handler(), "/healthz")
	hz, _ = healthKeys(t, rec.Body.Bytes())
	if rec.Code != http.StatusServiceUnavailable || hz["status"] != "unavailable" || hz["reason"] != BeforeV4Reason || hz["builtAt"] != nil {
		t.Errorf("unavailable: %d %s", rec.Code, rec.Body)
	}
}

// A database that cannot be reached: the connect error names its address,
// login and database. The public /healthz shows none of them; the admin one
// shows the error (SEC-HEALTH-1).
func TestPublicHealthHidesADatabaseError(t *testing.T) {
	t.Parallel()
	cfg, err := pgxpool.ParseConfig("postgres://atlas_reader@127.0.0.1:1/atlas_private?connect_timeout=1&sslmode=disable")
	if err != nil {
		t.Fatal(err)
	}
	pool, err := pgxpool.NewWithConfig(context.Background(), cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	s := New(DBSource{Pool: pool, Lists: store.NewLists()},
		Options{Logger: slog.New(slog.NewTextHandler(io.Discard, nil)), FilesDir: t.TempDir()})
	if _, err := s.Refresh(context.Background(), true); err == nil {
		t.Fatal("refresh against an unreachable database: no error")
	}
	secrets := []string{"127.0.0.1", "atlas_reader", "atlas_private"}
	admin := get(s.AdminHandler(), "/healthz").Body.String()
	for _, x := range secrets {
		if !strings.Contains(admin, x) {
			t.Errorf("admin /healthz lacks %q: %s", x, admin)
		}
	}
	rec := get(s.Handler(), "/healthz")
	for _, x := range secrets {
		if strings.Contains(rec.Body.String(), x) {
			t.Errorf("public /healthz shows %q: %s", x, rec.Body)
		}
	}
	if _, keys := healthKeys(t, rec.Body.Bytes()); rec.Code != http.StatusServiceUnavailable || !slices.Equal(keys, publicKeys) {
		t.Errorf("public: %d %s", rec.Code, rec.Body)
	}
}
