package store_test

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/ice-zeus/milf-website/api/internal/config"
	"github.com/ice-zeus/milf-website/api/internal/store"
	"github.com/ice-zeus/milf-website/api/internal/testdb"
)

func demoFile(t *testing.T) string {
	t.Helper()
	f := config.FindUp(filepath.Join("fixtures", "demo", "design-demo.json"))
	if f == "" {
		t.Fatal("fixtures/demo/design-demo.json not found above the test directory")
	}
	return f
}

func count(t *testing.T, db *testdb.DB, sql string) int {
	t.Helper()
	c := db.Conn(t)
	defer c.Close(context.Background())
	var n int
	if err := c.QueryRow(context.Background(), sql).Scan(&n); err != nil {
		t.Fatalf("%s: %v", sql, err)
	}
	return n
}

func TestMigrateIsIdempotentAndChecksummed(t *testing.T) {
	db := testdb.New(t) // already migrated once
	ctx := context.Background()
	c := db.Conn(t)
	defer c.Close(ctx)
	res, err := store.Migrate(ctx, c)
	if err != nil {
		t.Fatal(err)
	}
	migrations, err := store.Migrations()
	if err != nil {
		t.Fatal(err)
	}
	if len(res.Applied) != 0 || res.Current != migrations[len(migrations)-1].Version {
		t.Fatalf("second migrate applied %v (current %d)", res.Applied, res.Current)
	}
	if err := store.CheckSchema(ctx, c); err != nil {
		t.Fatal(err)
	}
	if n := count(t, db, "SELECT count(*) FROM information_schema.tables WHERE table_schema='site'"); n < 16 {
		t.Fatalf("site schema has %d tables", n)
	}
	// Writes bump the revision, deletes included.
	before := count(t, db, "SELECT value FROM site.revision")
	db.Exec(t, "INSERT INTO site.targets(target_id,label) VALUES ('x','X')")
	db.Exec(t, "DELETE FROM site.targets WHERE target_id='x'")
	if after := count(t, db, "SELECT value FROM site.revision"); after != before+2 {
		t.Fatalf("revision %d -> %d, want +2", before, after)
	}
	// An edited migration is refused, by migrate and by the read-only check.
	db.Exec(t, "UPDATE site.schema_versions SET checksum='tampered' WHERE version=1")
	if _, err := store.Migrate(ctx, c); err == nil || !strings.Contains(err.Error(), "checksum") {
		t.Fatalf("tampered checksum accepted: %v", err)
	}
	if err := store.CheckSchema(ctx, c); err == nil {
		t.Fatal("CheckSchema accepted a tampered checksum")
	}
}

func TestSeedAndUnseedAreIdempotent(t *testing.T) {
	db := testdb.New(t)
	ctx := context.Background()
	c := db.Conn(t)
	defer c.Close(ctx)
	file := demoFile(t)
	first, err := store.SeedDemo(ctx, c, file, false)
	if err != nil {
		t.Fatal(err)
	}
	if first.Skipped {
		t.Fatal("first seed skipped")
	}
	want := map[string]int{"providers": 4, "tape_events": 49, "changelog": 31, "reviews": 32, "community_runs": 25, "measurements": 8, "pricing": 8, "trust_signals": 8, "provider_products": 11, "pricing_plans": 48}
	for table, n := range want {
		if first.Rows[table] != n {
			t.Errorf("seeded %d rows into %s, want %d", first.Rows[table], table, n)
		}
		if got := count(t, db, "SELECT count(*) FROM site."+table+" WHERE is_demo"); got != n {
			t.Errorf("site.%s has %d demo rows, want %d", table, got, n)
		}
	}
	revision := count(t, db, "SELECT value FROM site.revision")
	second, err := store.SeedDemo(ctx, c, file, false)
	if err != nil {
		t.Fatal(err)
	}
	if !second.Skipped || count(t, db, "SELECT value FROM site.revision") != revision {
		t.Fatalf("reseeding the same file was not a no-op: %+v", second)
	}
	forced, err := store.SeedDemo(ctx, c, file, true)
	if err != nil {
		t.Fatal(err)
	}
	for table, n := range want {
		if forced.Rows[table] != n || count(t, db, "SELECT count(*) FROM site."+table+" WHERE is_demo") != n {
			t.Errorf("forced reseed: site.%s has %d rows, want %d", table, forced.Rows[table], n)
		}
	}
	catalog := count(t, db, "SELECT count(*) FROM site.scopes") + count(t, db, "SELECT count(*) FROM site.ranking_profiles") + count(t, db, "SELECT count(*) FROM site.score_weights")
	if catalog != 5+5+8 {
		t.Fatalf("catalog rows %d, want 18", catalog)
	}
	deleted, err := store.UnseedDemo(ctx, c)
	if err != nil {
		t.Fatal(err)
	}
	if deleted["providers"] != 4 {
		t.Fatalf("unseed deleted %v", deleted)
	}
	demoRows := 0
	for _, table := range []string{"datasets", "targets", "providers", "provider_products", "pricing", "pricing_plans", "trust_signals", "measurements", "reviews", "community_runs", "changelog", "tape_events"} {
		demoRows += count(t, db, "SELECT count(*) FROM site."+table+" WHERE is_demo")
	}
	if demoRows != 0 {
		t.Fatalf("%d demo rows left after unseed", demoRows)
	}
	// Reference catalog stays: scopes, weights, profiles and the framework's own targets.
	if count(t, db, "SELECT count(*) FROM site.scopes")+count(t, db, "SELECT count(*) FROM site.ranking_profiles")+count(t, db, "SELECT count(*) FROM site.score_weights") != catalog {
		t.Fatal("unseed removed catalog rows")
	}
	if got := count(t, db, "SELECT count(*) FROM site.targets"); got != 2 {
		t.Fatalf("site.targets has %d rows after unseed, want the 2 framework targets", got)
	}
	again, err := store.UnseedDemo(ctx, c)
	if err != nil {
		t.Fatal(err)
	}
	if again["providers"] != 0 {
		t.Fatal("second unseed deleted rows")
	}
	if res, err := store.SeedDemo(ctx, c, file, false); err != nil || res.Skipped {
		t.Fatalf("seed after unseed: %+v %v", res, err)
	}
}

func TestBackupWritesARestorableDump(t *testing.T) {
	db := testdb.New(t)
	runtime := config.FindUp(filepath.Join("milf", ".state", "postgres", "runtime.json"))
	if runtime == "" {
		t.Skip("no milf/.state/postgres/runtime.json with pg_dump")
	}
	bin, err := store.PgBinDir(runtime)
	if err != nil {
		t.Fatal(err)
	}
	dir := t.TempDir()
	res, err := store.Backup(db.DSN, bin, dir, "test")
	if err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(res.Path)
	if err != nil || info.Size() == 0 || info.Mode().Perm() != 0o600 {
		t.Fatalf("dump %s: %v %v", res.Path, info, err)
	}
	if _, err := os.Stat(filepath.Join(filepath.Dir(res.Path), "backup.json")); err != nil {
		t.Fatal(err)
	}
}
