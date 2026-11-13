package store

import (
	"context"
	"errors"
	"slices"
	"time"

	"github.com/jackc/pgx/v5"
)

// ErrUnknownCursor is a before cursor that names no event: a page older than
// it has no place to start (/v1/events answers 400 unknown_cursor).
var ErrUnknownCursor = errors.New("unknown_cursor: before names no event")

// anchorSQL reads the event a before cursor names.
const anchorSQL = `SELECT ` + EventColumns + ` WHERE e.event_id=$1`

// queryBefore is QueryEvents with before (EVENTS-BEFORE-1). The page stands
// below where the anchor stood on the page before it: its own place (time,
// then id), or for a merged answer its group's newest row's (the merged row
// carries the group's largest id, which need not be its newest row). So it
// reads the anchor; for a merged answer the rows of its group above it
// (slotRowsQuery); the Limit+1 newest rows below the anchor's group's place
// (GroupRows times as many for a merged answer, TextScan with q); and the
// rows at or above that place in the slots of the merging rows read, so the
// build leaves out a group standing above the page as it does the anchor's.
// The rows come newest first; the build places the anchor (build.matching).
func queryBefore(ctx context.Context, tx pgx.Tx, f EventFilter) ([]Event, error) {
	anchor, err := collect(ctx, tx, anchorSQL, ScanEvent, *f.Before)
	if err != nil {
		return nil, err
	}
	if len(anchor) == 0 {
		return nil, ErrUnknownCursor
	}
	top := anchor[0]
	if f.Merged() && top.Merges() {
		sql, args := slotRowsQuery(groupFilter(f, &top), []time.Time{top.Group().Slot}, ">", top.At, top.ID)
		above, err := collect(ctx, tx, sql, ScanEvent, args...)
		if err != nil {
			return nil, err
		}
		for i := range above {
			if above[i].Group() == top.Group() && Newer(&above[i], &top) {
				top = above[i]
			}
		}
		anchor = append(anchor, above...)
	}
	sql, args := beforeQuery(f, top.At, top.ID)
	rows, err := collect(ctx, tx, sql, ScanEvent, args...)
	if err != nil {
		return nil, err
	}
	if slots := mergingSlots(rows); f.Merged() && len(slots) > 0 {
		sql, args := slotRowsQuery(f, slots, ">=", top.At, top.ID)
		above, err := collect(ctx, tx, sql, ScanEvent, args...)
		if err != nil {
			return nil, err
		}
		anchor = append(anchor, above...)
	}
	return newestFirst(append(rows, anchor...)), nil
}

// beforeQuery is the query of the readLimits newest rows passing f's clauses
// below the place (at, id), and its arguments.
func beforeQuery(f EventFilter, at time.Time, id int64) (string, []any) {
	clauses, args := eventClauses(f)
	args = append(args, at, id)
	clauses = append(clauses, "(e.at, e.event_id)<("+placeholders(len(args)-1, 2)+")")
	limit, _ := readLimits(f)
	return eventsQuery(clauses, args, limit)
}

// groupFilter is f narrowed to e's kind, product and country, so the read
// of e's group's rows above it is not filled by other groups of its slot.
func groupFilter(f EventFilter, e *Event) EventFilter {
	s := func(p *string) string {
		if p == nil {
			return ""
		}
		return *p
	}
	f.Kinds, f.Country = []string{e.Kind}, s(e.Country)
	if e.Provider != nil {
		f.Product = *e.Provider + "/" + s(e.Product)
	}
	return f
}

// mergingSlots is the slots of rows that merge (Event.Merges).
func mergingSlots(rows []Event) []time.Time {
	slots := []time.Time{}
	for i := range rows {
		if rows[i].Merges() {
			slots = append(slots, rows[i].Group().Slot)
		}
	}
	return slots
}

// newestFirst is rows sorted newest first (Newer), each id once.
func newestFirst(rows []Event) []Event {
	slices.SortFunc(rows, func(a, b Event) int {
		if a.ID == b.ID {
			return 0
		}
		if Newer(&a, &b) {
			return -1
		}
		return 1
	})
	return slices.CompactFunc(rows, func(a, b Event) bool { return a.ID == b.ID })
}
