package store

import (
	"context"
	"fmt"

	"github.com/jackc/pgx/v5"
)

// MethodologyRow is one public.methodologies row: a methodology id and its
// definition as the database prints it (the jsonb as text). The calculator
// writes one row per distinct id and never deletes one, so every id a
// snapshot ever published has its row (METHODOLOGY-FILE-1, #1179).
type MethodologyRow struct {
	ID         string
	Definition []byte
}

// MethodologiesGap is the SiteGaps line while the API role cannot read
// public.methodologies (dbserver/api_tables.txt, granted by `db-server
// check`): the build then serves no methodology file, and every
// snapshot.methodologyUrl is null.
const MethodologiesGap = "public.methodologies: the API role has no SELECT on it, so no methodology file is served (GRANT SELECT ON public.methodologies)"

const (
	methodologiesGrantSQL = `SELECT has_table_privilege('public.methodologies', 'SELECT')`
	methodologiesSQL      = `SELECT methodology_id, definition::text FROM public.methodologies ORDER BY methodology_id`
)

// loadMethodologies reads every methodology row, or none (and the
// MethodologiesGap) while the API role cannot read the table, so a binary
// deployed before the grant still builds.
func (in *Inputs) loadMethodologies(ctx context.Context, tx pgx.Tx) error {
	var readable bool
	if err := tx.QueryRow(ctx, methodologiesGrantSQL).Scan(&readable); err != nil {
		return fmt.Errorf("methodologies grant: %w", err)
	}
	if !readable {
		in.Methodologies = []MethodologyRow{}
		in.SiteGaps = append(in.SiteGaps, MethodologiesGap)
		return nil
	}
	rows, err := collect(ctx, tx, methodologiesSQL, func(r pgx.Rows, v *MethodologyRow) error { return r.Scan(&v.ID, &v.Definition) })
	if err != nil {
		return fmt.Errorf("methodologies: %w", err)
	}
	in.Methodologies = rows
	return nil
}
