package store

import (
	"context"
	"slices"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
)

// The detector judges each pulse series per target, so one outage seen by the
// generic_http and the generic_https pulses of a slot is two events. Every
// list a page reads shows it as one row (EVENTS-MERGE-1): the build merges a
// group's rows (build.mergeRefs), and the counts take the extra rows off
// (eventDuplicatesSQL). The stored rows are untouched.

// Merges reports whether e merges with the other rows of its group into one
// row per incident: a row of a non-routine kind (pulse down and recovered,
// incidents, audits) with a target. Routine rows stay one per target: each
// target's pulse is its own measurement.
func (e *Event) Merges() bool { return !Routine(e.Kind) && e.Target != nil }

// Merged reports whether f's answer shows one row per incident (Event.Merges):
// every filter but target=, which gets that target's own rows.
func (f EventFilter) Merged() bool { return f.Target == "" }

// GroupKey is the incident a merging row belongs to: its kind,
// network, country and region, and its pulse slot (Event.Slot), or without a
// batch its own time.
type GroupKey struct {
	Kind, Provider, Product, Country, Region string
	Slot                                     time.Time
}

// Group is e's GroupKey.
func (e *Event) Group() GroupKey {
	s := func(p *string) string {
		if p == nil {
			return ""
		}
		return *p
	}
	slot := e.At
	if e.Slot != nil {
		slot = *e.Slot
	}
	return GroupKey{e.Kind, s(e.Provider), s(e.Product), s(e.Country), s(e.Region), slot.UTC()}
}

// GroupRows is the most rows one merged row is read from, for the reads that
// must find a group's rows past a limit (the database fallback, the home
// tape's heads). Two targets pulse today; four leaves room.
const GroupRows = 4

// categorySQL is Category in SQL for the row alias e.
const categorySQL = `CASE WHEN left(e.kind,7)='census_'
 OR e.kind IN ('daily_test_started','daily_test_finished','estimate_hour_released') THEN 'census'
 WHEN left(e.kind,6)='pulse_' THEN 'pulse'
 WHEN e.kind IN ('new_hosting_asn','ip_overlap') THEN 'audit' ELSE 'incident' END`

// eventDuplicatesSQL counts, per 30-minute bucket, network and tab, the rows
// from $1 to $2 that a merged row stands for beside its newest one (n > 1),
// with their good and bad signals: what the counts take off
// (loadEventStats). Only merging rows are read, on test_events_notable.
const eventDuplicatesSQL = `SELECT date_bin('30 minutes', d.at, '2000-01-01 00:00+00'), p.provider, d.category,
 count(*)::int, (count(*) FILTER (WHERE d.signal > 0))::int, (count(*) FILTER (WHERE d.signal < 0))::int
 FROM (SELECT e.at, e.product_id, e.signal, ` + categorySQL + ` AS category, row_number() OVER (PARTITION BY e.kind,
 e.product_id, e.country_code, coalesce(e.region_id::text, e.data->>'region'), coalesce(b.scheduled_for, e.at)
 ORDER BY e.at DESC, e.event_id DESC) AS n
 FROM public.test_events e LEFT JOIN public.test_batches b ON b.batch_id = e.batch_id
 WHERE ` + notableClause + ` AND e.target_id IS NOT NULL AND e.at >= $1 AND e.at < $2) d
 LEFT JOIN public.products p USING (product_id) WHERE d.n > 1 GROUP BY 1, 2, 3`

// readLimits is how many rows the fallback reads: the newest (or with after
// the oldest past it) and the newest non-routine ones; GroupRows times as
// many when the build merges the answer (Merged), so a group at the limit is
// read whole, plus one, which tells whether more rows follow.
func readLimits(f EventFilter) (rows, notable int) {
	n := 1
	if f.Merged() {
		n = GroupRows
	}
	switch {
	case len(f.Words) > 0:
		return TextScan, TextScan
	case f.After != nil, f.Before != nil:
		return f.Limit*n + 1, 0
	}
	return f.Limit*n + 1, NotableQuota(f.Limit) * n
}

// groupsBelow is the database fallback's read, for a merged after answer, of
// the merging rows at or below after in the slots of rows (the rows read past
// it), so a group whose newest id is past after is answered whole: the
// detector writes a slot's batches one target at a time, so a group's ids
// interleave with other groups'.
func groupsBelow(ctx context.Context, tx pgx.Tx, f EventFilter, rows []Event) ([]Event, error) {
	slots := mergingSlots(rows)
	if len(slots) == 0 {
		return nil, nil
	}
	sql, args := groupsBelowQuery(f, slots)
	return collect(ctx, tx, sql, ScanEvent, args...)
}

// groupsBelowQuery is groupsBelow's query and its arguments: f's clauses but
// after, at most GroupRows rows per slot.
func groupsBelowQuery(f EventFilter, slots []time.Time) (string, []any) {
	return slotRowsQuery(f, slots, "<=", *f.After)
}

// slotRowsQuery is the query of the merging rows passing f's clauses but its
// cursors, in slots, whose key (e.event_id, or the row's time and id) passes
// cmp against bound, and its arguments: on test_events_notable from the
// oldest slot (a row is written after its slot starts), at most GroupRows
// rows per slot.
func slotRowsQuery(f EventFilter, slots []time.Time, cmp string, bound ...any) (string, []any) {
	g := f
	g.After, g.Before = nil, nil
	clauses, args := eventClauses(g)
	if !g.nonRoutine() {
		clauses = append(clauses, notableClause)
	}
	key := "e.event_id"
	if len(bound) == 2 {
		key = "(e.at, e.event_id)"
	}
	args = append(args, bound...)
	clauses = append(clauses, "e.target_id IS NOT NULL", key+cmp+"("+placeholders(len(args)-len(bound)+1, len(bound))+")")
	args = append(args, slices.MinFunc(slots, func(a, b time.Time) int { return a.Compare(b) }), slots)
	n := len(args)
	clauses = append(clauses, "e.at>=$"+itoa(n-1), "coalesce(b.scheduled_for, e.at)=ANY($"+itoa(n)+")")
	return eventsQuery(clauses, args, len(slots)*GroupRows)
}

// placeholders is n numbered placeholders from $from, comma-separated.
func placeholders(from, n int) string {
	out := make([]string, n)
	for i := range out {
		out[i] = "$" + itoa(from+i)
	}
	return strings.Join(out, ", ")
}
