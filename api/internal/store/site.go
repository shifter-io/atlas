package store

import (
	"context"
	"fmt"
	"slices"
	"sort"
	"strings"
)

// The site schema (created by `siteapi migrate`) is optional at read time: a
// database that has only the framework's schema, or a site schema missing a
// table or a column this binary reads, is served with those site rows empty.
// SiteLayout is what the database has, read from the catalog; Load reads a
// site table only when every column it selects exists and reports the others
// in Inputs.SiteGaps.

// SiteLayout is the site schema's tables and their columns.
type SiteLayout map[string]map[string]bool

const siteLayoutSQL = `SELECT c.relname::text, a.attname::text
 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
 JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
 WHERE n.nspname = 'site' AND c.relkind IN ('r', 'p', 'v', 'm', 'f')`

// ReadSiteLayout lists the site schema's tables and columns (none when the
// schema does not exist).
func ReadSiteLayout(ctx context.Context, q Querier) (SiteLayout, error) {
	rows, err := q.Query(ctx, siteLayoutSQL)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	l := SiteLayout{}
	for rows.Next() {
		var table, column string
		if err := rows.Scan(&table, &column); err != nil {
			return nil, err
		}
		if l[table] == nil {
			l[table] = map[string]bool{}
		}
		l[table][column] = true
	}
	return l, rows.Err()
}

// missing is the columns of table the layout lacks (every one when the table
// itself is missing).
func (l SiteLayout) missing(table string, columns []string) []string {
	have := l[table]
	out := []string{}
	for _, c := range columns {
		if !have[c] {
			out = append(out, c)
		}
	}
	return out
}

// has reports whether table exists with column.
func (l SiteLayout) has(table, column string) bool { return l[table][column] }

// siteRead is one optional site table read: its columns (the SELECT list) and
// the ORDER BY, then the columns a later migration added (later). Rows a
// fixture loader once marked is_demo are never served.
type siteRead struct {
	table   string
	columns []string
	order   string
	later   []laterColumn
}

// laterColumn is a column of a later migration and its type: a database
// without it reads it as null, and the table's other columns as they are.
type laterColumn struct{ name, typ string }

func (r siteRead) sql(l SiteLayout) string {
	cols := append([]string{}, r.columns...)
	for _, c := range r.later {
		if l.has(r.table, c.name) {
			cols = append(cols, c.name)
		} else {
			cols = append(cols, "NULL::"+c.typ+" AS "+c.name)
		}
	}
	q := "SELECT " + strings.Join(cols, ", ") + " FROM site." + r.table
	if l.has(r.table, "is_demo") {
		q += " WHERE NOT is_demo"
	}
	if r.order != "" {
		q += " ORDER BY " + r.order
	}
	return q
}

// gap describes why r is not read, or what it reads as null; "" when it
// reads every column.
func (r siteRead) gap(l SiteLayout) string {
	miss := r.missing(l)
	switch {
	case l[r.table] == nil:
		return "site." + r.table + ": table missing"
	case len(miss) > 0:
		return "site." + r.table + ": missing columns " + strings.Join(miss, ", ")
	}
	for _, c := range r.later {
		if !l.has(r.table, c.name) {
			miss = append(miss, c.name)
		}
	}
	if len(miss) == 0 {
		return ""
	}
	return "site." + r.table + ": missing columns " + strings.Join(miss, ", ") + " (read as null)"
}

// missing is the selected and ordering columns the layout lacks.
func (r siteRead) missing(l SiteLayout) []string {
	cols := append([]string{}, r.columns...)
	for _, o := range strings.Split(r.order, ",") {
		if f := strings.Fields(o); len(f) > 0 && !slices.Contains(cols, f[0]) {
			cols = append(cols, f[0])
		}
	}
	return l.missing(r.table, cols)
}

// SiteTables is every site table the API reads, the order the fingerprint
// lists them in.
var SiteTables = []string{"targets", "scopes", "score_weights", "ranking_profiles", "providers", "provider_products",
	"pricing", "pricing_plans", "trust_signals", "reviews", "community_runs", "changelog", "tape_events", "pricing_history"}

// siteFingerprintSQL is the part of the fingerprint the site schema supplies:
// the revision counter its triggers bump, the newest updated_at of the tables
// the API reads, and which of them are readable (so `siteapi migrate` on a
// running server is noticed).
func siteFingerprintSQL(l SiteLayout) (revision, updated, readable string) {
	revision = "0"
	if l.has("revision", "value") {
		revision = "(SELECT value FROM site.revision)"
	}
	parts := []string{}
	names := []string{}
	for _, t := range SiteTables {
		if l[t] == nil {
			continue
		}
		names = append(names, t)
		if l.has(t, "updated_at") {
			parts = append(parts, "SELECT max(updated_at) u FROM site."+t)
		}
	}
	updated = "NULL::timestamptz"
	if len(parts) > 0 {
		updated = "(SELECT max(u) FROM (" + strings.Join(parts, " UNION ALL ") + ") x)"
	}
	sort.Strings(names)
	return revision, updated, strings.Join(names, ",")
}

// siteGaps lists every read the layout cannot serve; one line for a missing
// site schema.
func siteGaps(l SiteLayout, reads []siteRead) []string {
	if len(l) == 0 {
		return []string{fmt.Sprintf("site schema missing: its %d tables are served empty (`siteapi migrate` creates them)", len(reads))}
	}
	out := []string{}
	for _, r := range reads {
		if g := r.gap(l); g != "" {
			out = append(out, g)
		}
	}
	return out
}
