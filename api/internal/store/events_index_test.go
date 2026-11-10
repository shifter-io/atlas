package store_test

import (
	"context"
	"strings"
	"testing"

	"github.com/jackc/pgx/v5"

	"github.com/Zeus-Development/proxy-index/api/internal/store"
	"github.com/Zeus-Development/proxy-index/api/internal/testdb"
)

// Every rare-value read can use its partial index (framework schema v15):
// with sequential scans off, its plan reads the index of test_events'
// partition. A notable clause that no longer repeats the index predicate, or
// a kinds or tab filter that drops it, fails here.
func TestRareEventReadsUseTheirPartialIndexes(t *testing.T) {
	t.Parallel()
	db := testdb.New(t)
	// A routine bulk and one rare row, analyzed: the plan is chosen by cost.
	db.Exec(t, `INSERT INTO test_events(at,kind) SELECT '2026-11-01T00:00:00Z'::timestamptz + n * interval '1 minute',
  'daily_test_finished' FROM generate_series(1,5000) n;
 INSERT INTO test_events(at,kind,signal,severity) VALUES ('2026-11-02T12:00:00Z','pulse_down',-1,'major');
 ANALYZE test_events`)
	ctx := context.Background()
	c := db.Conn(t)
	defer c.Close(ctx)
	notable := "test_events_default_at_event_id_idx"
	for _, tc := range []struct {
		name    string
		f       store.EventFilter
		notable bool
		index   string
	}{
		{"notable read", store.EventFilter{Limit: 100}, true, notable},
		{"non-routine kinds", store.EventFilter{Kinds: []string{"pulse_down", "pulse_recovered"}, Limit: 100}, false, notable},
		{"incident tab", store.EventFilter{Category: "incident", Limit: 100}, false, notable},
		{"audit tab", store.EventFilter{Category: "audit", Limit: 100}, false, notable},
		{"country", store.EventFilter{Country: "DE", Limit: 100}, false, "test_events_default_country_code_at_idx"},
		{"target", store.EventFilter{Target: "example", Limit: 100}, false, "test_events_default_target_id_at_idx"},
	} {
		sql, args := store.EventsQuery(tc.f, tc.notable)
		if plan := planWithoutSeqScan(t, c, sql, args); !strings.Contains(plan, tc.index) {
			t.Errorf("%s does not read %s:\n%s", tc.name, tc.index, plan)
		}
	}
}

// Only filters that admit no routine row repeat the notable clause.
func TestRoutineFiltersKeepTheirRows(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		f    store.EventFilter
		want bool
	}{
		{store.EventFilter{Kinds: []string{"pulse_down"}}, true},
		{store.EventFilter{Kinds: []string{"pulse_down", "pulse_finished"}}, false},
		{store.EventFilter{Kinds: []string{"census_started"}}, false},
		{store.EventFilter{Category: "pulse"}, false},
		{store.EventFilter{Category: "census"}, false},
		{store.EventFilter{Category: "incident"}, true},
		{store.EventFilter{Category: "audit"}, true},
		{store.EventFilter{Country: "DE"}, false},
	} {
		sql, _ := store.EventsQuery(tc.f, false)
		if got := strings.Contains(sql, "NOT (left(e.kind,7)"); got != tc.want {
			t.Errorf("%+v: notable clause %v, want %v", tc.f, got, tc.want)
		}
	}
}

func planWithoutSeqScan(t *testing.T, c *pgx.Conn, sql string, args []any) string {
	t.Helper()
	ctx := context.Background()
	tx, err := c.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback(ctx) //nolint:errcheck
	if _, err := tx.Exec(ctx, "SET LOCAL enable_seqscan = off"); err != nil {
		t.Fatal(err)
	}
	rows, err := tx.Query(ctx, "EXPLAIN "+sql, args...)
	if err != nil {
		t.Fatal(err)
	}
	lines, err := pgx.CollectRows(rows, pgx.RowTo[string])
	if err != nil {
		t.Fatal(err)
	}
	return strings.Join(lines, "\n")
}
