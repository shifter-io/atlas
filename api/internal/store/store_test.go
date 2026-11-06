package store_test

import (
	"context"
	"slices"
	"strings"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/store"
	"github.com/Zeus-Development/proxy-index/api/internal/testdb"
)

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

func load(t *testing.T, db *testdb.DB) *store.Inputs {
	t.Helper()
	ctx := context.Background()
	pool, err := store.OpenReadOnly(ctx, db.DSN)
	if err != nil {
		t.Fatal(err)
	}
	// A v4 build's lists read their rows through the pool while it is built.
	t.Cleanup(pool.Close)
	in, err := store.Load(ctx, pool)
	if err != nil {
		t.Fatal(err)
	}
	return in
}

func fingerprint(t *testing.T, db *testdb.DB) store.Fingerprint {
	t.Helper()
	ctx := context.Background()
	pool, err := store.OpenReadOnly(ctx, db.DSN)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	f, err := store.ReadFingerprint(ctx, pool)
	if err != nil {
		t.Fatal(err)
	}
	return f
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
	// A database migrated by an older siteapi, whose 0001 differed only in a
	// comment: migrate rewrites its checksum to the current one.
	db.Exec(t, "UPDATE site.schema_versions SET checksum='8127cc64a2088bbd3667a4a55dee6f66e79b1d0c271354469aadc8e9c84741ec' WHERE version=1")
	if res, err := store.Migrate(ctx, c); err != nil || len(res.Rechecked) != 1 || res.Rechecked[0] != migrations[0].Name {
		t.Fatalf("migrate over a comment-only checksum: %+v %v", res, err)
	}
	if n := count(t, db, "SELECT count(*) FROM site.schema_versions WHERE version=1 AND checksum='"+migrations[0].Checksum+"'"); n != 1 {
		t.Fatal("checksum not rewritten")
	}
	// An edited migration is refused.
	db.Exec(t, "UPDATE site.schema_versions SET checksum='tampered' WHERE version=1")
	if _, err := store.Migrate(ctx, c); err == nil || !strings.Contains(err.Error(), "checksum") {
		t.Fatalf("tampered checksum accepted: %v", err)
	}
}

// A database with only the framework's schema (no `siteapi migrate` yet) is
// read with every site table empty, named once in SiteGaps; migrating it
// changes the fingerprint, so a running server rebuilds.
func TestLoadReadsADatabaseWithoutTheSiteSchema(t *testing.T) {
	db := testdb.NewFramework(t)
	in := load(t, db)
	if len(in.SiteGaps) != 1 || !strings.Contains(in.SiteGaps[0], "site schema missing") || !strings.Contains(in.SiteGaps[0], "siteapi migrate") {
		t.Fatalf("site gaps %q", in.SiteGaps)
	}
	if len(in.Providers)+len(in.Products)+len(in.Pricing)+len(in.Plans)+len(in.Trust)+len(in.Reviews)+len(in.CommunityRuns)+
		len(in.Changelog)+len(in.Tape)+len(in.Targets)+len(in.Scopes)+len(in.Weights)+len(in.Profiles) != 0 {
		t.Fatal("site rows read from a database without a site schema")
	}
	if len(in.FrameworkTargets) != 2 {
		t.Fatalf("framework targets %d, want 2", len(in.FrameworkTargets))
	}
	before := fingerprint(t, db)
	if before.SiteTables != "" || before.SiteRevision != 0 || before.SiteUpdatedAt != nil {
		t.Fatalf("fingerprint without a site schema %+v", before)
	}
	c := db.Conn(t)
	defer c.Close(context.Background())
	if _, err := store.Migrate(context.Background(), c); err != nil {
		t.Fatal(err)
	}
	if after := fingerprint(t, db); after.Key() == before.Key() || after.SiteTables == "" {
		t.Fatalf("migrating did not change the fingerprint: %+v", after)
	}
	if gaps := load(t, db).SiteGaps; len(gaps) != 0 {
		t.Fatalf("site gaps after migrate %q", gaps)
	}
}

// A missing table or column empties only that table's rows; rows a fixture
// loader marked is_demo are never read.
func TestLoadSkipsWhatTheSiteSchemaLacksAndDemoRows(t *testing.T) {
	db := testdb.New(t)
	db.Exec(t, `INSERT INTO site.providers(slug,name,framework_provider) VALUES ('acme','Acme','acme');
INSERT INTO site.providers(slug,name,is_demo) VALUES ('nimbus','Nimbus',true);
INSERT INTO site.pricing(provider_slug,category,price_per_gb) VALUES ('acme','residential',3);
INSERT INTO site.pricing_plans(provider_slug,category,position,name) VALUES ('acme','residential',0,'Starter');
INSERT INTO site.reviews(review_id,provider_slug,author,role,rating,title,body,product_kind,use_case,monthly_spend,months_used,verification,posted_at)
 VALUES ('r1','acme','A','dev',4,'t','b','residential_rotating','scraping','$100',2,'unverified',now());
DROP TABLE site.changelog;
ALTER TABLE site.pricing_plans DROP COLUMN catalog;`)
	in := load(t, db)
	if len(in.Providers) != 1 || in.Providers[0].Slug != "acme" {
		t.Fatalf("providers %+v (the demo row must be skipped)", in.Providers)
	}
	if len(in.Pricing) != 1 || len(in.Reviews) != 1 || len(in.Plans) != 0 || len(in.Changelog) != 0 {
		t.Fatalf("pricing %d reviews %d plans %d changelog %d", len(in.Pricing), len(in.Reviews), len(in.Plans), len(in.Changelog))
	}
	want := []string{"site.pricing_plans: missing columns catalog", "site.changelog: table missing"}
	for _, w := range want {
		if !slices.Contains(in.SiteGaps, w) {
			t.Errorf("site gaps %q lack %q", in.SiteGaps, w)
		}
	}
	if len(in.SiteGaps) != len(want) {
		t.Errorf("site gaps %q, want %q", in.SiteGaps, want)
	}
	if f := fingerprint(t, db); strings.Contains(f.SiteTables, "changelog") || !strings.Contains(f.SiteTables, "pricing_plans") {
		t.Errorf("fingerprint site tables %q", f.SiteTables)
	}
}
