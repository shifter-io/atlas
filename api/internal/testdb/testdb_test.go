package testdb

import (
	"context"
	"encoding/hex"
	"fmt"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
)

// The database tests load the framework schema from the repository's own
// framework tree, src/proxy_index three folders up, not from a pinned copy.
func TestFrameworkDirIsTheFrameworkTree(t *testing.T) {
	got := FrameworkDir()
	if got == "" {
		t.Fatal("FrameworkDir found no framework tree")
	}
	gotInfo, err := os.Stat(got)
	if err != nil {
		t.Fatal(err)
	}
	wantInfo, err := os.Stat(filepath.Join("..", "..", "..", "src", "proxy_index"))
	if err != nil {
		t.Fatal(err)
	}
	if !os.SameFile(gotInfo, wantInfo) {
		t.Fatalf("FrameworkDir = %q, want the repository's src/proxy_index", got)
	}
	for _, file := range []string{"schema.sql", filepath.Join("calculated", "schema.sql")} {
		if _, err := os.Stat(filepath.Join(got, file)); err != nil {
			t.Errorf("framework schema: %v", err)
		}
	}
}

// A template database name holds the Unix time it was made; the sweep drops
// those over templateMaxAge old and never a name it cannot read.
func TestTemplateExpired(t *testing.T) {
	t.Parallel()
	now := time.Unix(1_800_000_000, 0)
	for name, want := range map[string]bool{
		templatePrefix + "1799990000_0a1b2c": true,
		templatePrefix + "1799999000_0a1b2c": false,
		templatePrefix + "1799990000":        false,
		templatePrefix + "soon_0a1b2c":       false,
		"siteapi_test_0a1b2c":                false,
	} {
		if got := templateExpired(name, now); got != want {
			t.Errorf("templateExpired(%q) = %v, want %v", name, got, want)
		}
	}
}

// Every database is a fresh copy of its template: what one test writes no
// other test's database has, New's has the site schema migrated and
// NewFramework's has only the framework schema and its reference rows.
func TestEachDatabaseIsAFreshCopy(t *testing.T) {
	t.Parallel()
	first := New(t)
	first.Exec(t, `INSERT INTO runner_regions(region_id,slug,pattern) VALUES (2,'ams3','ams3-*')`)
	second, framework := New(t), NewFramework(t)
	for _, c := range []struct {
		db   *DB
		sql  string
		want string
	}{
		{first, `SELECT count(*)::text FROM runner_regions`, "2"},
		{second, `SELECT count(*)::text FROM runner_regions`, "1"},
		{second, `SELECT (count(*) > 0)::text FROM site.schema_versions`, "true"},
		{framework, `SELECT count(*)::text FROM targets`, "2"},
		{framework, `SELECT (to_regclass('site.schema_versions') IS NULL)::text`, "true"},
	} {
		conn := c.db.Conn(t)
		var got string
		err := conn.QueryRow(context.Background(), c.sql).Scan(&got)
		conn.Close(context.Background())
		if err != nil || got != c.want {
			t.Errorf("%s on %s: %q, %v; want %q", c.sql, c.db.Name, got, err, c.want)
		}
	}
}

// The sweep drops a template database left over an hour ago and keeps a
// newer one.
func TestSweepDropsOnlyOldTemplates(t *testing.T) {
	t.Parallel()
	admin := adminConfig(t)
	ctx := context.Background()
	now := time.Now()
	old := fmt.Sprintf("%s%d_%s", templatePrefix, now.Add(-2*templateMaxAge).Unix(), hex.EncodeToString(randomBytes()[:6]))
	recent := fmt.Sprintf("%s%d_%s", templatePrefix, now.Unix(), hex.EncodeToString(randomBytes()[:6]))
	for _, name := range []string{old, recent} {
		if err := adminExec(ctx, admin, "CREATE DATABASE "+pgx.Identifier{name}.Sanitize()); err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { adminExec(ctx, admin, dropSQL(name)) }) //nolint:errcheck
	}
	sweepTemplates(admin, now)
	c, err := pgx.Connect(ctx, admin.DSN())
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close(ctx)
	var left []string
	rows, err := c.Query(ctx, "SELECT datname FROM pg_database WHERE datname = ANY($1) ORDER BY datname", []string{old, recent})
	if err == nil {
		left, err = pgx.CollectRows(rows, pgx.RowTo[string])
	}
	if err != nil || len(left) != 1 || left[0] != recent {
		t.Errorf("after the sweep: %v, %v; want only %s", left, err, recent)
	}
}
