// Package testdb creates throwaway Postgres databases for integration tests.
//
// Tests that need a database call New, which skips the test unless
// PG_TEST_ADMIN_FILE names a credentials TOML (e.g. .secrets/postgres-admin.toml)
// for a role allowed to CREATE DATABASE; with PG_TEST_REQUIRED set (CI) a
// missing PG_TEST_ADMIN_FILE fails the test instead. Each test gets its own database named
// siteapi_test_<random>, with the framework schema read from the repository's own
// framework tree (src/proxy_index), and dropped afterwards: a copy of a template
// database each test process loads once (template.go). The real proxy_reviews
// database is never touched.
package testdb

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/Zeus-Development/proxy-index/api/internal/config"
	"github.com/jackc/pgx/v5"
)

// DB is one scratch database.
type DB struct {
	Name  string
	DSN   string
	Admin config.Postgres
}

// FrameworkDir locates the framework tree, src/proxy_index in the repository
// root, or returns "" when there is none.
func FrameworkDir() string {
	return config.RepoPath(config.FrameworkTree)
}

// New creates a scratch database with the framework schema and the site
// schema migrated, a copy of this process's site template database (template.go).
// Skips when PG_TEST_ADMIN_FILE is unset.
func New(t testing.TB) *DB {
	t.Helper()
	admin := adminConfig(t)
	return create(t, admin, siteTemplate(t, admin))
}

// NewFramework creates a scratch database with the framework schema and its
// reference rows only: no site schema, as a database looks before `siteapi
// migrate` first runs. It is a copy of this process's framework template
// database (template.go). Skips when PG_TEST_ADMIN_FILE is unset.
func NewFramework(t testing.TB) *DB {
	t.Helper()
	admin := adminConfig(t)
	return create(t, admin, frameworkTemplate(t, admin))
}

// adminConfig is the admin role PG_TEST_ADMIN_FILE names. It skips the test
// when that is unset (fails it under PG_TEST_REQUIRED) and fails it when
// there is no framework tree to read the schema from.
func adminConfig(t testing.TB) config.Postgres {
	t.Helper()
	file := os.Getenv("PG_TEST_ADMIN_FILE")
	if file == "" && os.Getenv("PG_TEST_REQUIRED") != "" {
		t.Fatal("PG_TEST_REQUIRED is set and PG_TEST_ADMIN_FILE is not: the database tests must run")
	}
	if file == "" {
		t.Skip("database tests skipped: set PG_TEST_ADMIN_FILE=<path to .secrets/postgres-admin.toml> to run them against a scratch database")
	}
	admin, err := config.ReadDatabaseFile(file)
	if err != nil {
		t.Fatal(err)
	}
	if FrameworkDir() == "" {
		t.Fatal("cannot find src/proxy_index for the framework schema: run the tests inside the repository")
	}
	return admin
}

// create makes a copy of the template database with a random name and drops
// it when the test ends.
func create(t testing.TB, admin config.Postgres, template string) *DB {
	t.Helper()
	name := "siteapi_test_" + hex.EncodeToString(randomBytes()[:6])
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	sql := "CREATE DATABASE " + pgx.Identifier{name}.Sanitize() + " TEMPLATE " + pgx.Identifier{template}.Sanitize()
	if err := adminExec(ctx, admin, sql); err != nil {
		t.Fatalf("create scratch database: %v", err)
	}
	db := &DB{Name: name, DSN: admin.WithDB(name).DSN(), Admin: admin}
	t.Cleanup(func() { db.drop(t) })
	return db
}

// drop removes the database, logging a failure.
func (d *DB) drop(t testing.TB) {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	if err := adminExec(ctx, d.Admin, dropSQL(d.Name)); err != nil {
		t.Logf("drop %s: %v", d.Name, err)
	}
}

// referenceRows are the scratch partitions (the framework creates monthly
// ones on demand) and the reference rows the real database has.
const referenceRows = `CREATE TABLE test_events_default PARTITION OF test_events DEFAULT;
INSERT INTO targets(target_id,label,url,pass_condition,sort_order) VALUES
 ('generic_http','Generic HTTP','http://checkip.amazonaws.com/','Response body is the exit address the target saw, and it parses as an IP.',10),
 ('generic_https','Generic HTTPS','https://checkip.amazonaws.com/','CONNECT tunnel established, certificate validates, and the body parses as an IP.',20);
INSERT INTO runner_regions(region_id,slug,pattern) VALUES (1,'fra1','fra1-*');
INSERT INTO countries(country_code,name,region_id,census_time) VALUES ('US','United States',1,'06:00'),('DE','Germany',1,'07:00');`

// loadFramework runs one of the framework's schema files (relative to
// FrameworkDir) on c.
func loadFramework(ctx context.Context, c *pgx.Conn, file string) error {
	sql, err := os.ReadFile(filepath.Join(FrameworkDir(), file))
	if err != nil {
		return err
	}
	if _, err := c.Exec(ctx, string(sql)); err != nil {
		return fmt.Errorf("framework %s: %w", file, err)
	}
	return nil
}

// calculatedSchema is the framework's calculated schema, which is idempotent.
var calculatedSchema = filepath.Join("calculated", "schema.sql")

// loadCalculated runs the framework's calculated schema on the database.
func (d *DB) loadCalculated(t testing.TB) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	c := d.Conn(t)
	defer c.Close(context.Background())
	if err := loadFramework(ctx, c, calculatedSchema); err != nil {
		t.Fatal(err)
	}
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
	ctx := context.Background()
	c := d.Conn(t)
	defer c.Close(ctx)
	var s struct {
		Products []json.RawMessage `json:"products"`
	}
	if err := json.Unmarshal(payload, &s); err != nil {
		t.Fatal(err)
	}
	id := d.register(t, c, payload, s.Products)
	if _, err := c.Exec(ctx, "INSERT INTO calculated_snapshots VALUES ($1,$2,$3)", id, "test-"+hex.EncodeToString(randomBytes()), payload); err != nil {
		t.Fatal(err)
	}
	current(t, c, id)
	return id
}

// partsTable is calculated_snapshot_parts as proxy-index calculated/schema.sql
// creates it from calculated v8 (contract K1), for a framework schema older
// than that.
const partsTable = `CREATE TABLE IF NOT EXISTS calculated_snapshot_parts (
 build_id bigint NOT NULL REFERENCES calculated_snapshots(build_id) DEFERRABLE INITIALLY DEFERRED,
 kind text NOT NULL CHECK(kind IN ('product','runs')), position integer NOT NULL CHECK(position>=0),
 part_id text NOT NULL, product_id integer, cohort text, content_hash text NOT NULL,
 part jsonb NOT NULL, CHECK((kind='product')=(product_id IS NOT NULL AND cohort IS NOT NULL)),
 PRIMARY KEY(build_id,kind,position)
) PARTITION BY LIST(build_id)`

// PublishParts stores a snapshot as calculated v8 does (contract K1): head in
// calculated_snapshots and the product groups and runs parts in the build's
// partition of calculated_snapshot_parts, as the current published
// calculation, registering its products like Publish. The head and the
// partition are committed first, then the parts are written over several
// connections at once (insertParts); the build is current only once all are.
func (d *DB) PublishParts(t testing.TB, head []byte, products, runs [][]byte) int64 {
	t.Helper()
	ctx := context.Background()
	c := d.Conn(t)
	defer c.Close(ctx)
	groups := make([]json.RawMessage, len(products))
	for i, p := range products {
		groups[i] = p
	}
	id := d.register(t, c, head, groups)
	tx, err := c.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback(ctx) //nolint:errcheck
	partition := pgx.Identifier{fmt.Sprintf("calculated_snapshot_parts_b%d", id)}.Sanitize()
	for _, sql := range []string{partsTable, fmt.Sprintf("CREATE TABLE %s PARTITION OF calculated_snapshot_parts FOR VALUES IN (%d)", partition, id)} {
		if _, err := tx.Exec(ctx, sql); err != nil {
			t.Fatalf("%v\n%s", err, sql)
		}
	}
	if _, err := tx.Exec(ctx, "INSERT INTO calculated_snapshots VALUES ($1,$2,$3)", id, "test-"+hex.EncodeToString(randomBytes()), head); err != nil {
		t.Fatal(err)
	}
	if err := tx.Commit(ctx); err != nil {
		t.Fatal(err)
	}
	if err := d.insertParts(ctx, id, products, runs); err != nil {
		t.Fatal(err)
	}
	current(t, c, id)
	return id
}

// partWriters is how many connections insertParts writes over: one
// connection takes about 20 s for a QA-1-sized build's 720 product parts
// (425 MB), most of it the server parsing their jsonb.
const partWriters = 8

// insertParts writes the build's product and runs parts from up to
// partWriters connections at once and returns the first error.
func (d *DB) insertParts(ctx context.Context, id int64, products, runs [][]byte) error {
	insert := "INSERT INTO calculated_snapshot_parts VALUES ($1,$2,$3,$4,$5,$6,'test',$7)"
	write := func(c *pgx.Conn, i int) error {
		if i >= len(products) {
			r := i - len(products)
			_, err := c.Exec(ctx, insert, id, "runs", r, fmt.Sprintf("runs:%d", r), nil, nil, runs[r])
			return err
		}
		var g struct {
			ID        string `json:"id"`
			ProductID int64  `json:"product_id"`
			Cohort    string `json:"cohort"`
		}
		if err := json.Unmarshal(products[i], &g); err != nil {
			return err
		}
		_, err := c.Exec(ctx, insert, id, "product", i, g.ID, g.ProductID, g.Cohort, products[i])
		return err
	}
	return d.inParallel(ctx, len(products)+len(runs), write)
}

// inParallel calls write for every index below n from up to partWriters
// connections at once and returns the first error.
func (d *DB) inParallel(ctx context.Context, n int, write func(*pgx.Conn, int) error) error {
	work := make(chan int, n)
	for i := range n {
		work <- i
	}
	close(work)
	writers := min(n, partWriters)
	errs := make(chan error, writers)
	for range writers {
		go func() { errs <- d.writeParts(ctx, work, write) }()
	}
	var first error
	for range writers {
		if err := <-errs; err != nil && first == nil {
			first = err
		}
	}
	return first
}

// writeParts writes, from one connection, each part taken from work.
func (d *DB) writeParts(ctx context.Context, work <-chan int, write func(*pgx.Conn, int) error) error {
	c, err := pgx.Connect(ctx, d.DSN)
	if err != nil {
		return err
	}
	defer c.Close(ctx)
	for i := range work {
		if err := write(c, i); err != nil {
			return err
		}
	}
	return nil
}

// register records the snapshot's methodology, providers and products and a
// published build for it; returns the build id.
func (d *DB) register(t testing.TB, c *pgx.Conn, head []byte, products []json.RawMessage) int64 {
	t.Helper()
	var s struct {
		AsOf        string `json:"as_of"`
		Methodology struct {
			ID       string          `json:"id"`
			Settings json.RawMessage `json:"settings"`
		} `json:"methodology"`
	}
	if err := json.Unmarshal(head, &s); err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	for _, raw := range products {
		var p struct {
			Provider string `json:"provider"`
			Product  string `json:"product"`
		}
		if err := json.Unmarshal(raw, &p); err != nil {
			t.Fatal(err)
		}
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
	if err := c.QueryRow(ctx, "INSERT INTO calculation_builds(methodology_id,as_of,input_hash,published) VALUES ($1,$2,$3,true) RETURNING build_id",
		s.Methodology.ID, s.AsOf, hex.EncodeToString(randomBytes())).Scan(&id); err != nil {
		t.Fatal(err)
	}
	return id
}

// current makes build the current calculation.
func current(t testing.TB, c *pgx.Conn, build int64) {
	t.Helper()
	if _, err := c.Exec(context.Background(), "INSERT INTO current_calculation VALUES (true,$1) ON CONFLICT (singleton) DO UPDATE SET build_id=excluded.build_id", build); err != nil {
		t.Fatal(err)
	}
}

func randomBytes() []byte {
	buf := make([]byte, 8)
	rand.Read(buf) //nolint:errcheck
	return buf
}
