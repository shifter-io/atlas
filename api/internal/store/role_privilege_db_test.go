package store_test

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/Zeus-Development/atlas/api/internal/store"
	"github.com/Zeus-Development/atlas/api/internal/testdb"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

// apiTables is the framework's list of the tables and views the API reads
// (dbserver/api_tables.txt), which `db-server check` grants the API role and
// nothing else in schema public.
func apiTables(t *testing.T) []string {
	t.Helper()
	text, err := os.ReadFile(filepath.Join(testdb.FrameworkDir(), "dbserver", "api_tables.txt"))
	if err != nil {
		t.Fatal(err)
	}
	var tables []string
	for _, line := range strings.Split(string(text), "\n") {
		if line = strings.TrimSpace(line); line != "" && !strings.HasPrefix(line, "#") {
			tables = append(tables, line)
		}
	}
	return tables
}

// allowlistRole adds a login role in the state `db-server check` leaves the
// API role: SELECT on the listed tables and views and on schema site, the
// role's own limits, and, for PUBLIC, no TEMPORARY on the database and no
// large-object creation. It returns the role's database config.
func allowlistRole(t *testing.T, db *testdb.DB) (string, func(dbname string) string) {
	t.Helper()
	role := "siteapi_api_" + strings.TrimPrefix(db.Name, "siteapi_test_")
	database := pgx.Identifier{db.Name}.Sanitize()
	db.Exec(t, fmt.Sprintf(`CREATE ROLE %[1]s LOGIN PASSWORD 'allowlist' CONNECTION LIMIT 20;
 ALTER ROLE %[1]s SET default_transaction_read_only = 'on'; ALTER ROLE %[1]s SET statement_timeout = '30s';
 ALTER ROLE %[1]s SET idle_in_transaction_session_timeout = '60s'; ALTER ROLE %[1]s SET temp_file_limit = '1GB';
 GRANT USAGE ON SCHEMA public, site TO %[1]s; GRANT SELECT ON ALL TABLES IN SCHEMA site TO %[1]s;
 GRANT SELECT ON %[2]s TO %[1]s; REVOKE TEMPORARY ON DATABASE %[3]s FROM PUBLIC;
 REVOKE EXECUTE ON FUNCTION pg_catalog.lo_creat(integer), pg_catalog.lo_create(oid), pg_catalog.lo_from_bytea(oid, bytea) FROM PUBLIC`,
		role, strings.Join(apiTables(t), ", "), database))
	// Before the database is dropped (cleanups run last first).
	t.Cleanup(func() { db.Exec(t, "DROP OWNED BY "+role+"; DROP ROLE "+role) })
	dsn := func(dbname string) string {
		cfg := db.Admin.WithDB(dbname)
		cfg.User, cfg.Password = role, "allowlist"
		return cfg.DSN()
	}
	return dsn(db.Name), dsn
}

// TestAPIRoleRunsEveryStatement: with exactly the listed grants, every
// statement of the explain registry runs, and the role's limits hold.
func TestAPIRoleRunsEveryStatement(t *testing.T) {
	t.Parallel()
	db := explainDB(t)
	ctx := context.Background()
	dsn, _ := allowlistRole(t, db)
	pool, err := store.OpenReadOnly(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	report, err := store.Explain(ctx, pool)
	if err != nil {
		t.Fatal(err)
	}
	if len(report.Statements) != len(store.Statements()) {
		t.Fatalf("%d statements explained, %d registered", len(report.Statements), len(store.Statements()))
	}
	for _, e := range report.Statements {
		if e.Error != "" {
			t.Errorf("%s failed with the API's grants: %s", e.Name, e.Error)
		}
	}
	// The role's own settings, as a client that asks for nothing gets them.
	c, err := pgx.Connect(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close(ctx)
	want := map[string]string{"default_transaction_read_only": "on", "statement_timeout": "30s",
		"idle_in_transaction_session_timeout": "1min", "temp_file_limit": "1GB"}
	for name, value := range want {
		var got string
		if err := c.QueryRow(ctx, "SELECT current_setting($1)", name).Scan(&got); err != nil || got != value {
			t.Errorf("%s = %q (%v), want %q", name, got, err, value)
		}
	}
	var limit int
	if err := c.QueryRow(ctx, "SELECT rolconnlimit FROM pg_roles WHERE rolname = current_user").Scan(&limit); err != nil || limit != 20 {
		t.Errorf("connection limit %d (%v), want 20", limit, err)
	}
}

// refusedTables are the framework tables the audit named that the API must
// not read: observed addresses, worker hosts, settings and run summaries.
var refusedTables = []string{"ip_addresses", "ip_observations", "ip_enrichment", "product_ips", "pool_ips",
	"scope_ips", "calculated_bucket_ips", "calculated_ip_last_seen", "nodes", "settings", "test_runs", "job_runs", "batch_runs"}

// TestAPIRoleIsRefused: with read-only switched off, the role reads no table
// or view off the list, no catalog secret and no server file, runs no
// program, makes no temporary table or large object, and connects to no other
// database whose CONNECT PUBLIC lost.
func TestAPIRoleIsRefused(t *testing.T) {
	t.Parallel()
	db := testdb.New(t)
	ctx := context.Background()
	dsn, elsewhere := allowlistRole(t, db)
	var unlisted []string
	c := db.Conn(t)
	rows, err := c.Query(ctx, `SELECT c.relname FROM pg_class c WHERE c.relnamespace = 'public'::regnamespace
 AND c.relkind IN ('r', 'p', 'v', 'm') AND NOT c.relispartition AND NOT 'public.' || c.relname = ANY($1) ORDER BY 1`, apiTables(t))
	if err == nil {
		unlisted, err = pgx.CollectRows(rows, pgx.RowTo[string])
	}
	c.Close(ctx)
	if err != nil {
		t.Fatal(err)
	}
	for _, name := range refusedTables {
		if !slices.Contains(unlisted, name) {
			t.Errorf("%s is not a framework table off the list", name)
		}
	}
	statements := []string{
		"SELECT * FROM pg_authid",
		"SELECT pg_read_file('PG_VERSION')",
		"SELECT pg_read_binary_file('PG_VERSION')",
		"SELECT pg_ls_dir('.')",
		"SELECT lo_import('PG_VERSION')",
		"COPY (SELECT 1) TO PROGRAM 'true'",
		"CREATE TEMP TABLE scratch (x integer)",
		"SELECT lo_create(0)",
		"SELECT lo_from_bytea(0, 'x')",
	}
	for _, name := range unlisted {
		statements = append(statements, "SELECT 1 FROM public."+pgx.Identifier{name}.Sanitize()+" LIMIT 0")
	}
	r, err := pgx.Connect(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer r.Close(ctx)
	if _, err := r.Exec(ctx, "SET default_transaction_read_only = off"); err != nil {
		t.Fatal(err)
	}
	for _, s := range statements {
		if _, err := r.Exec(ctx, s); !refused(err) {
			t.Errorf("%s: %v, want permission denied", s, err)
		}
	}

	other := testdb.NewFramework(t)
	o, err := pgx.Connect(ctx, elsewhere(other.Name))
	if err != nil {
		t.Fatalf("before CONNECT is revoked from PUBLIC: %v", err)
	}
	o.Close(ctx)
	db.Exec(t, "REVOKE CONNECT ON DATABASE "+pgx.Identifier{other.Name}.Sanitize()+" FROM PUBLIC")
	if o, err := pgx.Connect(ctx, elsewhere(other.Name)); !refused(err) {
		if o != nil {
			o.Close(ctx)
		}
		t.Errorf("connect to another database: %v, want permission denied", err)
	}
}

// refused is a permission error (insufficient_privilege).
func refused(err error) bool {
	var pg *pgconn.PgError
	return errors.As(err, &pg) && pg.Code == "42501"
}
