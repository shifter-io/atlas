package store_test

import (
	"context"
	"strings"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/store"
	"github.com/Zeus-Development/proxy-index/api/internal/testdb"
	"github.com/Zeus-Development/proxy-index/api/internal/testscale"
)

// Framework targets carry their engine, in sort order, and a changed engine
// changes the fingerprint (so the answers are rebuilt).
func TestFrameworkTargetsCarryTheirEngine(t *testing.T) {
	t.Parallel()
	db := testdb.New(t)
	db.Exec(t, testscale.BrowserTargetsSQL())
	in := load(t, db)
	if len(in.FrameworkTargets) != 2+len(testscale.BrowserTargets) {
		t.Fatalf("%d framework targets", len(in.FrameworkTargets))
	}
	for i, ft := range in.FrameworkTargets {
		want := "browser"
		if i < 2 {
			want = "http"
		} else if ft.ID != testscale.BrowserTargets[i-2].ID {
			t.Errorf("target %d is %s, want %s", i, ft.ID, testscale.BrowserTargets[i-2].ID)
		}
		if ft.Engine != want {
			t.Errorf("%s engine %q, want %q", ft.ID, ft.Engine, want)
		}
	}
	before := fingerprint(t, db)
	db.Exec(t, "UPDATE targets SET engine='http' WHERE target_id='amazon_pdp'")
	if after := fingerprint(t, db); after.DataKey() == before.DataKey() {
		t.Error("an engine change did not change the fingerprint")
	}
}

// public.targets.engine exists since framework schema v1; a database without
// it is refused (the server keeps serving its previous version), never read
// as if every target were http.
func TestADatabaseWithoutTheEngineColumnIsRefused(t *testing.T) {
	t.Parallel()
	db := testdb.New(t)
	db.Exec(t, "ALTER TABLE targets DROP COLUMN engine CASCADE")
	ctx := context.Background()
	pool, err := store.OpenReadOnly(ctx, db.DSN)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	if _, err := store.ReadFingerprint(ctx, pool); err == nil || !strings.Contains(err.Error(), "engine") {
		t.Errorf("fingerprint without targets.engine: %v", err)
	}
	if _, err := store.Load(ctx, pool); err == nil || !strings.Contains(err.Error(), "engine") {
		t.Errorf("load without targets.engine: %v", err)
	}
}
