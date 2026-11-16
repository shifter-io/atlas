package store_test

import (
	"context"
	"slices"
	"strings"
	"testing"

	"github.com/Zeus-Development/atlas/api/internal/store"
	"github.com/Zeus-Development/atlas/api/internal/testdb"
)

// loadAs loads every input through the role's database config.
func loadAs(t *testing.T, dsn string) *store.Inputs {
	t.Helper()
	ctx := context.Background()
	pool, err := store.OpenReadOnly(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	in, err := store.Load(ctx, pool)
	if err != nil {
		t.Fatal(err)
	}
	return in
}

// With the API's grants, Load reads every methodology row; a role without
// SELECT on public.methodologies (a binary deployed before the grant) still
// loads, with no row and the gap named for /healthz siteGaps.
func TestLoadReadsTheMethodologiesWhenGranted(t *testing.T) {
	t.Parallel()
	db := testdb.New(t)
	db.Exec(t, `INSERT INTO methodologies VALUES ('m1', '{"weights": {"speed": 0.15}}'), ('m2', '{}')`)
	dsn, _ := allowlistRole(t, db)
	in := loadAs(t, dsn)
	if len(in.Methodologies) != 2 || in.Methodologies[0].ID != "m1" || !strings.Contains(string(in.Methodologies[0].Definition), `"speed": 0.15`) ||
		slices.Contains(in.SiteGaps, store.MethodologiesGap) {
		t.Errorf("methodologies %v, gaps %v", in.Methodologies, in.SiteGaps)
	}
	role := strings.TrimPrefix(db.Name, "siteapi_test_")
	db.Exec(t, "REVOKE SELECT ON public.methodologies FROM siteapi_api_"+role)
	in = loadAs(t, dsn)
	if len(in.Methodologies) != 0 || !slices.Contains(in.SiteGaps, store.MethodologiesGap) {
		t.Errorf("without the grant: methodologies %v, gaps %v", in.Methodologies, in.SiteGaps)
	}
}
