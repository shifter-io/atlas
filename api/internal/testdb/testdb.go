// Package testdb creates throwaway Postgres databases for integration tests.
//
// Tests that need a database call New, which skips the test unless
// PG_TEST_ADMIN_FILE names a credentials TOML (e.g. proxy-index/.secrets/postgres-admin.toml)
// for a role allowed to CREATE DATABASE. Each test gets its own database named
// siteapi_test_<random>, loaded with the framework schema read from the proxy-index/
// checkout (PG_TEST_FRAMEWORK_DIR overrides), and dropped afterwards. The real
// proxy_reviews database is never touched.
package testdb

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/Zeus-Development/proxy-index-website/api/internal/config"
	"github.com/Zeus-Development/proxy-index-website/api/internal/store"
	"github.com/jackc/pgx/v5"
)

// DB is one scratch database.
type DB struct {
	Name  string
	DSN   string
	Admin config.Postgres
}

// FrameworkDir locates proxy-index/src/proxy_index.
func FrameworkDir() string {
	if d := os.Getenv("PG_TEST_FRAMEWORK_DIR"); d != "" {
		return d
	}
	return config.FrameworkPath("src", "proxy_index")
}

// New creates a scratch database with the framework schema and the site
// schema migrated. Skips when PG_TEST_ADMIN_FILE is unset.
func New(t testing.TB) *DB {
	t.Helper()
	db := NewFramework(t)
	w := db.Conn(t)
	defer w.Close(context.Background())
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	if _, err := store.Migrate(ctx, w); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	return db
}

// NewFramework creates a scratch database with the framework schema and its
// reference rows only: no site schema, as a database looks before `siteapi
// migrate` first runs. Skips when PG_TEST_ADMIN_FILE is unset.
func NewFramework(t testing.TB) *DB {
	t.Helper()
	file := os.Getenv("PG_TEST_ADMIN_FILE")
	if file == "" {
		t.Skip("database tests skipped: set PG_TEST_ADMIN_FILE=<path to proxy-index/.secrets/postgres-admin.toml> to run them against a scratch database")
	}
	admin, err := config.ReadDatabaseFile(file)
	if err != nil {
		t.Fatal(err)
	}
	fw := FrameworkDir()
	if fw == "" {
		t.Fatal("cannot find proxy-index/src/proxy_index for the framework schema; set PG_TEST_FRAMEWORK_DIR")
	}
	buf := make([]byte, 6)
	rand.Read(buf) //nolint:errcheck
	name := "siteapi_test_" + hex.EncodeToString(buf)
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	conn, err := pgx.Connect(ctx, admin.DSN())
	if err != nil {
		t.Fatalf("connect as admin: %v", err)
	}
	if _, err := conn.Exec(ctx, "CREATE DATABASE "+pgx.Identifier{name}.Sanitize()); err != nil {
		conn.Close(ctx)
		t.Fatalf("create scratch database: %v", err)
	}
	conn.Close(ctx)
	db := &DB{Name: name, DSN: admin.WithDB(name).DSN(), Admin: admin}
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()
		c, err := pgx.Connect(ctx, admin.DSN())
		if err != nil {
			t.Logf("drop %s: %v", name, err)
			return
		}
		defer c.Close(ctx)
		if _, err := c.Exec(ctx, "DROP DATABASE IF EXISTS "+pgx.Identifier{name}.Sanitize()+" WITH (FORCE)"); err != nil {
			t.Logf("drop %s: %v", name, err)
		}
	})
	w := db.Conn(t)
	defer w.Close(context.Background())
	for _, f := range []string{"schema.sql", filepath.Join("calculated", "schema.sql")} {
		sql, err := os.ReadFile(filepath.Join(fw, f))
		if err != nil {
			t.Fatal(err)
		}
		if _, err := w.Exec(ctx, string(sql)); err != nil {
			t.Fatalf("framework %s: %v", f, err)
		}
	}
	// Scratch partitions (the framework creates monthly ones on demand) and the
	// reference rows the real database has.
	if _, err := w.Exec(ctx, `CREATE TABLE test_events_default PARTITION OF test_events DEFAULT;
INSERT INTO targets(target_id,label,url,pass_condition,sort_order) VALUES
 ('generic_http','Generic HTTP','http://checkip.amazonaws.com/','Response body is the exit address the target saw, and it parses as an IP.',10),
 ('generic_https','Generic HTTPS','https://checkip.amazonaws.com/','CONNECT tunnel established, certificate validates, and the body parses as an IP.',20);
INSERT INTO runner_regions(region_id,slug,pattern) VALUES (1,'fra1','fra1-*');
INSERT INTO countries(country_code,name,region_id,census_time) VALUES ('US','United States',1,'06:00'),('DE','Germany',1,'07:00');`); err != nil {
		t.Fatalf("scratch reference rows: %v", err)
	}
	return db
}

// Conn opens a read-write connection.
func (d *DB) Conn(t testing.TB) *pgx.Conn {
	t.Helper()
	c, err := pgx.Connect(context.Background(), d.DSN)
	if err != nil {
		t.Fatal(err)
	}
	return c
}

// Exec runs SQL or fails the test.
func (d *DB) Exec(t testing.TB, sql string, args ...any) {
	t.Helper()
	c := d.Conn(t)
	defer c.Close(context.Background())
	if _, err := c.Exec(context.Background(), sql, args...); err != nil {
		t.Fatalf("%v\n%s", err, sql)
	}
}

// Publish stores a snapshot payload as the current published calculation,
// registering its products in the framework's providers/products tables.
func (d *DB) Publish(t testing.TB, payload []byte) int64 {
	t.Helper()
	var s struct {
		AsOf        string `json:"as_of"`
		Methodology struct {
			ID       string          `json:"id"`
			Settings json.RawMessage `json:"settings"`
		} `json:"methodology"`
		Products []struct {
			Provider string `json:"provider"`
			Product  string `json:"product"`
		} `json:"products"`
	}
	if err := json.Unmarshal(payload, &s); err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	c := d.Conn(t)
	defer c.Close(ctx)
	for _, p := range s.Products {
		if _, err := c.Exec(ctx, "INSERT INTO providers VALUES ($1) ON CONFLICT DO NOTHING", p.Provider); err != nil {
			t.Fatal(err)
		}
		if _, err := c.Exec(ctx, "INSERT INTO products(provider,name) VALUES ($1,$2) ON CONFLICT DO NOTHING", p.Provider, p.Product); err != nil {
			t.Fatal(err)
		}
	}
	settings := s.Methodology.Settings
	if len(settings) == 0 {
		settings = json.RawMessage("{}")
	}
	if _, err := c.Exec(ctx, "INSERT INTO methodologies VALUES ($1,$2) ON CONFLICT DO NOTHING", s.Methodology.ID, settings); err != nil {
		t.Fatal(err)
	}
	var id int64
	buf := make([]byte, 8)
	rand.Read(buf) //nolint:errcheck
	if err := c.QueryRow(ctx, "INSERT INTO calculation_builds(methodology_id,as_of,input_hash,published) VALUES ($1,$2,$3,true) RETURNING build_id",
		s.Methodology.ID, s.AsOf, hex.EncodeToString(buf)).Scan(&id); err != nil {
		t.Fatal(err)
	}
	if _, err := c.Exec(ctx, "INSERT INTO calculated_snapshots VALUES ($1,$2,$3)", id, "test-"+hex.EncodeToString(buf), payload); err != nil {
		t.Fatal(err)
	}
	if _, err := c.Exec(ctx, "INSERT INTO current_calculation VALUES (true,$1) ON CONFLICT (singleton) DO UPDATE SET build_id=excluded.build_id", id); err != nil {
		t.Fatal(err)
	}
	return id
}
