package server

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"os"
	"strings"
	"testing"

	"github.com/shifter-io/atlas/api/internal/build"
	"github.com/shifter-io/atlas/api/internal/config"
	"github.com/shifter-io/atlas/api/internal/store"
	"github.com/shifter-io/atlas/api/internal/testdb"
	"github.com/shifter-io/atlas/api/internal/testscale"
)

// guardDB is a database serving the browser scores head (its methodology
// groups instagram_web and x_web as social_web) with the framework's targets
// and the whole catalog, as a deploy seeds them.
func guardDB(t *testing.T) *testdb.DB {
	t.Helper()
	head, err := os.ReadFile(config.FindUp(browserScoresHead))
	if err != nil {
		t.Fatal(err)
	}
	db := testdb.New(t)
	db.Exec(t, testscale.HeadTargetsSQL())
	db.Publish(t, head)
	return db
}

func guardServer(t *testing.T, db *testdb.DB) *Server {
	t.Helper()
	pool, err := store.OpenReadOnly(context.Background(), db.DSN)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(pool.Close)
	return New(DBSource{Pool: pool}, Options{Logger: slog.New(slog.NewTextHandler(io.Discard, nil)), FilesDir: t.TempDir()})
}

// dropSocial is the staging database of 16 Nov 2026 (SEED-ON-DEPLOY-1,
// #1284): the methodology's social_web group, but no social_web target row in
// the framework or the catalog.
const dropSocial = "DELETE FROM site.targets WHERE target_id = 'social_web'; DELETE FROM targets WHERE target_id = 'social_web'"

// A catalog without a group the methodology publishes is never served: a
// serving siteapi keeps its version and reports the refusal (degraded, the
// server's alert), a fresh one never becomes healthy (the deploy does not
// switch to it), and the seed's rows bring the group back.
func TestAGroupMissingFromTheCatalogIsNeverServed(t *testing.T) {
	t.Parallel()
	ctx := context.Background()
	db := guardDB(t)
	s := guardServer(t, db)
	if _, err := s.Refresh(ctx, true); err != nil {
		t.Fatal(err)
	}
	served := get(s.Handler(), "/v1/meta").Body.String()
	if !strings.Contains(served, `"id":"social_web"`) {
		t.Fatal("the complete catalog does not serve social_web")
	}

	db.Exec(t, dropSocial)
	if _, err := s.Refresh(ctx, false); !errors.Is(err, build.ErrCatalogIncomplete) || !strings.Contains(err.Error(), "social_web (no target row)") {
		t.Fatalf("an incomplete catalog was built: %v", err)
	}
	if get(s.Handler(), "/v1/meta").Body.String() != served {
		t.Fatal("the served version changed")
	}
	if h := health(t, s); h.Status != "degraded" || h.FailedError == nil || !strings.Contains(*h.FailedError, "atlas db-seed --only targets") {
		t.Fatalf("health during the refusal %+v", h)
	}
	fresh := guardServer(t, db) // the deploy's new colour on the same database
	if _, err := fresh.Refresh(ctx, true); !errors.Is(err, build.ErrCatalogIncomplete) {
		t.Fatalf("a fresh server built an incomplete catalog: %v", err)
	}
	if h := health(t, fresh); h.Status == "ok" {
		t.Fatalf("a fresh server on an incomplete catalog is healthy: %+v", h)
	}

	// The deploy's seed: the framework's row and the catalog's (api/scripts/catalog-targets.sql).
	db.Exec(t, "INSERT INTO targets(target_id,label,url,engine,census,pulse_enabled,sort_order) VALUES "+
		"('social_web','Social media web app','https://www.instagram.com/','browser',false,false,55)")
	catalog, err := os.ReadFile(config.FindUp("api/scripts/catalog-targets.sql"))
	if err != nil {
		t.Fatal(err)
	}
	db.Exec(t, string(catalog))
	if _, err := s.Refresh(ctx, false); err != nil {
		t.Fatal(err)
	}
	if h := health(t, s); h.Status != "ok" || !strings.Contains(get(s.Handler(), "/v1/meta").Body.String(), `"id":"social_web"`) {
		t.Fatalf("after the seed: %+v", h)
	}
}
