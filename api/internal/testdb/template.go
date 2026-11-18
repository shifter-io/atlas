package testdb

import (
	"context"
	"encoding/hex"
	"fmt"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/shifter-io/atlas/api/internal/config"
	"github.com/shifter-io/atlas/api/internal/store"
)

// Template databases: each test process (one package's tests) loads the
// framework schema once, into a template database, and migrates the site
// schema once, into a second one made from the first; every test's database
// is a fresh copy of one of them (CREATE DATABASE ... TEMPLATE), so a test
// still gets a database of its own without loading the schema again. The
// templates are named siteapi_tmpl_<unix time>_<random>; a process cannot
// drop its own when it exits, so each process drops those over an hour old
// before it makes its first (a test process never runs that long).
const (
	templatePrefix = "siteapi_tmpl_"
	templateMaxAge = time.Hour
)

// template is one of this process's template databases, made by the first
// test that needs it.
type template struct {
	once sync.Once
	name string
	err  error
}

var frameworkDB, siteDB template

// frameworkTemplate is the template with the framework schema and its
// reference rows.
func frameworkTemplate(t testing.TB, admin config.Postgres) string {
	t.Helper()
	frameworkDB.once.Do(func() {
		sweepTemplates(admin, time.Now())
		frameworkDB.name, frameworkDB.err = makeTemplate(admin, "template1", loadSchema)
	})
	if frameworkDB.err != nil {
		t.Fatalf("framework template database: %v", frameworkDB.err)
	}
	return frameworkDB.name
}

// siteTemplate is the framework template with the site schema migrated.
func siteTemplate(t testing.TB, admin config.Postgres) string {
	t.Helper()
	from := frameworkTemplate(t, admin)
	siteDB.once.Do(func() {
		siteDB.name, siteDB.err = makeTemplate(admin, from, func(ctx context.Context, c *pgx.Conn) error {
			_, err := store.Migrate(ctx, c)
			return err
		})
	})
	if siteDB.err != nil {
		t.Fatalf("site template database: %v", siteDB.err)
	}
	return siteDB.name
}

// loadSchema loads the framework schema, its calculated schema and the
// reference rows.
func loadSchema(ctx context.Context, c *pgx.Conn) error {
	for _, file := range []string{"schema.sql", calculatedSchema} {
		if err := loadFramework(ctx, c, file); err != nil {
			return err
		}
	}
	if _, err := c.Exec(ctx, referenceRows); err != nil {
		return fmt.Errorf("scratch reference rows: %w", err)
	}
	return nil
}

// makeTemplate creates a template database as a copy of from and runs fill
// on it; on failure it drops the database again.
func makeTemplate(admin config.Postgres, from string, fill func(context.Context, *pgx.Conn) error) (string, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	name := templatePrefix + strconv.FormatInt(time.Now().Unix(), 10) + "_" + hex.EncodeToString(randomBytes()[:6])
	if err := adminExec(ctx, admin, "CREATE DATABASE "+pgx.Identifier{name}.Sanitize()+" TEMPLATE "+pgx.Identifier{from}.Sanitize()); err != nil {
		return "", err
	}
	err := fillDatabase(ctx, admin.WithDB(name), fill)
	if err != nil {
		adminExec(ctx, admin, dropSQL(name)) //nolint:errcheck // the sweep drops it later
		return "", err
	}
	return name, nil
}

// fillDatabase runs fill on the database and closes the connection, so the
// database can be a template (a template has no connections).
func fillDatabase(ctx context.Context, db config.Postgres, fill func(context.Context, *pgx.Conn) error) error {
	c, err := pgx.Connect(ctx, db.DSN())
	if err != nil {
		return err
	}
	defer c.Close(context.Background())
	return fill(ctx, c)
}

// sweepTemplates drops the template databases made over templateMaxAge
// before now, which their processes left behind. Best effort: a failure
// only leaves them for the next sweep.
func sweepTemplates(admin config.Postgres, now time.Time) {
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	c, err := pgx.Connect(ctx, admin.DSN())
	if err != nil {
		return
	}
	defer c.Close(context.Background())
	rows, err := c.Query(ctx, "SELECT datname FROM pg_database WHERE starts_with(datname, $1)", templatePrefix)
	if err != nil {
		return
	}
	names, err := pgx.CollectRows(rows, pgx.RowTo[string])
	if err != nil {
		return
	}
	for _, name := range names {
		if templateExpired(name, now) {
			c.Exec(ctx, dropSQL(name)) //nolint:errcheck // best effort
		}
	}
}

// templateExpired reports whether the template database name was made over
// templateMaxAge before now; a name it cannot read is not expired.
func templateExpired(name string, now time.Time) bool {
	made, _, ok := strings.Cut(strings.TrimPrefix(name, templatePrefix), "_")
	unix, err := strconv.ParseInt(made, 10, 64)
	return ok && err == nil && now.Sub(time.Unix(unix, 0)) > templateMaxAge
}

// adminExec runs sql on a new admin connection.
func adminExec(ctx context.Context, admin config.Postgres, sql string) error {
	c, err := pgx.Connect(ctx, admin.DSN())
	if err != nil {
		return fmt.Errorf("connect as admin: %w", err)
	}
	defer c.Close(context.Background())
	_, err = c.Exec(ctx, sql)
	return err
}

func dropSQL(name string) string {
	return "DROP DATABASE IF EXISTS " + pgx.Identifier{name}.Sanitize() + " WITH (FORCE)"
}
