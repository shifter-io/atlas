package store

import "time"

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
