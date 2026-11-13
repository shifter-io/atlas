package server_test

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

	"github.com/Zeus-Development/atlas/api/internal/config"
	"github.com/Zeus-Development/atlas/api/internal/server"
	"github.com/Zeus-Development/atlas/api/internal/store"
	"github.com/Zeus-Development/atlas/api/internal/testdb"
	"github.com/Zeus-Development/atlas/api/internal/testscale"
)

// publishV3 publishes the every-request fixture as a v3 head stored in parts.
func publishV3(t *testing.T, db *testdb.DB) {
	t.Helper()
	raw, err := os.ReadFile(config.FindUp(filepath.Join("fixtures", "measured", "snapshot-every-request.json")))
	if err != nil {
		t.Fatal(err)
	}
	head, products, runs, err := testscale.Parts(raw, false)
	if err != nil {
		t.Fatal(err)
	}
	var doc map[string]json.RawMessage
	if err := json.Unmarshal(head, &doc); err != nil {
		t.Fatal(err)
	}
	doc["schema_version"] = json.RawMessage("3")
	if head, err = json.Marshal(doc); err != nil {
		t.Fatal(err)
	}
	db.PublishParts(t, head, products, runs)
}

// A current build older than payload v4 is not served: every data route
// answers 503 with BeforeV4Reason, as does /healthz with its status and
// reason, from the first build on (siteapi starts anyway) and after a v4
// build served. A v4 build serves again, without the reason.
func TestABuildBeforeV4AnswersUnavailable(t *testing.T) {
	t.Parallel()
	db := testdb.New(t)
	ctx := context.Background()
	pool, err := store.OpenReadOnly(ctx, db.DSN)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	s := server.New(server.DBSource{Pool: pool, Lists: store.NewLists()},
		server.Options{Logger: slog.New(slog.NewTextHandler(io.Discard, nil)), FilesDir: t.TempDir()})
	h := s.Handler()
	unavailable := func(when string) {
		t.Helper()
		if _, err := s.Refresh(ctx, true); !errors.Is(err, store.ErrBeforeV4) {
			t.Fatalf("%s: refresh %v, want ErrBeforeV4", when, err)
		}
		for _, p := range []string{"/v1/networks", "/v1/networks/shifter", "/v1/dataset", "/v1/search?q=shifter", "/v1/events"} {
			rec := httptest.NewRecorder()
			h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, p, nil))
			var body struct{ Error string }
			if rec.Code != http.StatusServiceUnavailable || json.Unmarshal(rec.Body.Bytes(), &body) != nil || body.Error != server.BeforeV4Reason {
				t.Errorf("%s: %s %d %s", when, p, rec.Code, rec.Body)
			}
		}
		code, hz := healthOf(t, h)
		if code != http.StatusServiceUnavailable || hz.Status != "unavailable" || hz.Reason == nil || *hz.Reason != server.BeforeV4Reason {
			t.Errorf("%s: /healthz %d %+v", when, code, hz)
		}
	}
	publishV3(t, db)
	unavailable("first build")
	snap, err := os.ReadFile(config.FindUp(filepath.Join("fixtures", "measured", "snapshot.json")))
	if err != nil {
		t.Fatal(err)
	}
	db.PublishV4(t, snap)
	if _, err := s.Refresh(ctx, true); err != nil {
		t.Fatal(err)
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/v1/networks", nil))
	if code, hz := healthOf(t, h); rec.Code != http.StatusOK || code != http.StatusOK || hz.Status == "unavailable" || hz.Reason != nil {
		t.Errorf("a v4 build: /v1/networks %d, /healthz %d %+v", rec.Code, code, hz)
	}
	publishV3(t, db)
	unavailable("after a v4 build")
}

func healthOf(t *testing.T, h http.Handler) (int, server.Health) {
	t.Helper()
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/healthz", nil))
	var hz server.Health
	if err := json.Unmarshal(rec.Body.Bytes(), &hz); err != nil {
		t.Fatalf("/healthz %d %s: %v", rec.Code, rec.Body, err)
	}
	return rec.Code, hz
}
