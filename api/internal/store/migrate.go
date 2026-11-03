package store

import (
	"context"
	"crypto/sha256"
	"embed"
	"encoding/hex"
	"fmt"
	"io/fs"
	"sort"
	"strconv"
	"strings"

	"github.com/jackc/pgx/v5"
)

//go:embed migrations/*.sql
var migrationFiles embed.FS

// advisory lock key shared by migrate and seed (the framework uses 817452901/2).
const lockKey = 817452950

// Migration is one embedded, versioned SQL file.
type Migration struct {
	Version  int
	Name     string
	SQL      string
	Checksum string
}

// commentOnlyEdits lists the earlier checksums of applied migrations whose
// files later changed in comments only: 0001's header names the Go module path,
// which moved twice (2026-10-24 and 2026-10-27). A database migrated by an
// older siteapi carries one of these checksums for the same schema, so it is
// accepted, and `siteapi migrate` rewrites it to the current one. Never add a
// checksum here for an SQL change: write a new migration.
var commentOnlyEdits = map[string][]string{
	"0001_site_schema.sql": {
		"78404cdaea81a0e58379d7ce47132b8b098f0f6df9c091ce50c7915b57ae8916", // header names ice-zeus/milf-website
		"8127cc64a2088bbd3667a4a55dee6f66e79b1d0c271354469aadc8e9c84741ec", // header names ice-zeus/proxy-index-website
	},
}

// SameSchema reports whether a migration applied with checksum sum has m's
// schema: the current file, or an earlier version that differed in comments.
func (m Migration) SameSchema(sum string) bool {
	if sum == m.Checksum {
		return true
	}
	for _, old := range commentOnlyEdits[m.Name] {
		if sum == old {
			return true
		}
	}
	return false
}

// Migrations lists the embedded migrations in version order.
func Migrations() ([]Migration, error) {
	entries, err := fs.ReadDir(migrationFiles, "migrations")
	if err != nil {
		return nil, err
	}
	var out []Migration
	for _, e := range entries {
		name := e.Name()
		prefix, _, ok := strings.Cut(name, "_")
		if !ok {
			return nil, fmt.Errorf("migration %s needs a NNNN_ prefix", name)
		}
		v, err := strconv.Atoi(prefix)
		if err != nil {
			return nil, fmt.Errorf("migration %s: %w", name, err)
		}
		body, err := migrationFiles.ReadFile("migrations/" + name)
		if err != nil {
			return nil, err
		}
		sum := sha256.Sum256(body)
		out = append(out, Migration{Version: v, Name: name, SQL: string(body), Checksum: hex.EncodeToString(sum[:])})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Version < out[j].Version })
	return out, nil
}

// MigrateResult reports what Migrate did.
type MigrateResult struct {
	Applied []string `json:"applied"`
	// Rechecked is the applied migrations whose stored checksum was an
	// earlier comment-only version of the file, now rewritten.
	Rechecked []string `json:"rechecked,omitempty"`
	Current   int      `json:"current_version"`
}

// Migrate applies pending migrations in one transaction under an advisory
// lock. An applied migration whose checksum differs from the embedded file is
// an error: edits need a new migration, exactly like the framework's schema.
func Migrate(ctx context.Context, conn *pgx.Conn) (MigrateResult, error) {
	migrations, err := Migrations()
	if err != nil {
		return MigrateResult{}, err
	}
	var res MigrateResult
	err = pgx.BeginFunc(ctx, conn, func(tx pgx.Tx) error {
		if _, err := tx.Exec(ctx, "SELECT pg_advisory_xact_lock($1)", lockKey); err != nil {
			return err
		}
		if _, err := tx.Exec(ctx, `CREATE SCHEMA IF NOT EXISTS site`); err != nil {
			return err
		}
		if _, err := tx.Exec(ctx, `CREATE TABLE IF NOT EXISTS site.schema_versions (
 version integer PRIMARY KEY, name text NOT NULL, checksum text NOT NULL,
 applied_at timestamptz NOT NULL DEFAULT now())`); err != nil {
			return err
		}
		applied := map[int]string{}
		rows, err := tx.Query(ctx, "SELECT version, checksum FROM site.schema_versions")
		if err != nil {
			return err
		}
		for rows.Next() {
			var v int
			var c string
			if err := rows.Scan(&v, &c); err != nil {
				return err
			}
			applied[v] = c
		}
		if err := rows.Err(); err != nil {
			return err
		}
		known := map[int]bool{}
		for _, m := range migrations {
			known[m.Version] = true
			if sum, ok := applied[m.Version]; ok {
				if !m.SameSchema(sum) {
					return fmt.Errorf("site schema migration %s was applied with checksum %s but the embedded file is %s; write a new migration instead of editing an applied one", m.Name, sum, m.Checksum)
				}
				if sum != m.Checksum {
					if _, err := tx.Exec(ctx, "UPDATE site.schema_versions SET checksum=$1 WHERE version=$2", m.Checksum, m.Version); err != nil {
						return err
					}
					res.Rechecked = append(res.Rechecked, m.Name)
				}
				continue
			}
			if _, err := tx.Exec(ctx, m.SQL); err != nil {
				return fmt.Errorf("apply %s: %w", m.Name, err)
			}
			if _, err := tx.Exec(ctx, "INSERT INTO site.schema_versions(version,name,checksum) VALUES ($1,$2,$3)", m.Version, m.Name, m.Checksum); err != nil {
				return err
			}
			res.Applied = append(res.Applied, m.Name)
		}
		for v := range applied {
			if !known[v] {
				return fmt.Errorf("database has site schema version %d that this binary does not know; upgrade siteapi", v)
			}
		}
		return nil
	})
	if err != nil {
		return res, err
	}
	if len(migrations) > 0 {
		res.Current = migrations[len(migrations)-1].Version
	}
	if res.Applied == nil {
		res.Applied = []string{}
	}
	return res, nil
}

// Querier is satisfied by pgx.Conn, pgx.Tx and pgxpool.Pool.
type Querier interface {
	Query(ctx context.Context, sql string, args ...any) (pgx.Rows, error)
	QueryRow(ctx context.Context, sql string, args ...any) pgx.Row
}

// CheckSchema verifies (read-only) that every embedded migration is applied
// with a matching checksum (or an earlier comment-only one, SameSchema).
func CheckSchema(ctx context.Context, q Querier) error {
	migrations, err := Migrations()
	if err != nil {
		return err
	}
	var exists bool
	if err := q.QueryRow(ctx, "SELECT to_regclass('site.schema_versions') IS NOT NULL").Scan(&exists); err != nil {
		return err
	}
	if !exists {
		return fmt.Errorf("site schema is missing: run `siteapi migrate`")
	}
	rows, err := q.Query(ctx, "SELECT version, checksum FROM site.schema_versions")
	if err != nil {
		return err
	}
	applied, err := pgx.CollectRows(rows, func(r pgx.CollectableRow) ([2]string, error) {
		var v int
		var c string
		err := r.Scan(&v, &c)
		return [2]string{strconv.Itoa(v), c}, err
	})
	if err != nil {
		return err
	}
	have := map[string]string{}
	for _, a := range applied {
		have[a[0]] = a[1]
	}
	for _, m := range migrations {
		sum, ok := have[strconv.Itoa(m.Version)]
		if !ok {
			return fmt.Errorf("site schema migration %s is not applied: run `siteapi migrate`", m.Name)
		}
		if !m.SameSchema(sum) {
			return fmt.Errorf("site schema migration %s was applied with checksum %s, which differs from this binary's %s: the database was migrated by a different siteapi build", m.Name, sum, m.Checksum)
		}
	}
	return nil
}
