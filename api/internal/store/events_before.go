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
// (groupRowsQuery); the Limit+1 newest rows below the anchor's group's place
// (GroupRows times as many for a merged answer, TextScan with q); and every
// row of the groups of the merging rows read, above the place or below the
// read, so the build leaves out a group standing above the page as it does
// the anchor's, and shows the others whole (readBelow).
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
		sql, args := groupRowsQuery(f, []GroupKey{top.Group()}, ">", top.At, top.ID)
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
	rows, err := readBelow(ctx, tx, f, &top)
	if err != nil {
		return nil, err
	}
	return newestFirst(append(rows, anchor...)), nil
}

// readBelow reads the rows below the place top: the readLimits newest, and
// every row of their merging rows' groups; then on below the last row read
// while the read was full and too few of its rows stand below the place
// alone: the groups with a row at or above it stood on an earlier page, and
// in a wide outage they can fill whole reads. With q one read, as the build
// matches the words (a full read says more).
func readBelow(ctx context.Context, tx pgx.Tx, f EventFilter, top *Event) ([]Event, error) {
	limit, _ := readLimits(f)
	out, read, at, id := []Event{}, map[GroupKey]bool{}, top.At, top.ID
	for {
		sql, args := beforeQuery(f, at, id)
		rows, err := collect(ctx, tx, sql, ScanEvent, args...)
		if err != nil {
			return nil, err
		}
		out = append(out, rows...)
		groups := []GroupKey{}
		for _, g := range mergingGroups(rows) {
			if !read[g] {
				groups, read[g] = append(groups, g), true
			}
		}
		if f.Merged() && len(groups) > 0 {
			sql, args := groupRowsQuery(f, groups, "")
			members, err := collect(ctx, tx, sql, ScanEvent, args...)
			if err != nil {
				return nil, err
			}
			out = append(out, members...)
		}
		if len(rows) < limit || len(f.Words) > 0 || pageRows(f, out, top) > f.Limit {
			return out, nil
		}
		at, id = rows[len(rows)-1].At, rows[len(rows)-1].ID
	}
}

// pageRows is how many rows of a before page rows give below the place top:
// one per row, or per group for merging rows of a merged answer, leaving out
// the groups with a row at or above top.
func pageRows(f EventFilter, rows []Event, top *Event) int {
	shown, groups, n := map[GroupKey]bool{}, map[GroupKey]bool{}, 0
	for i := range rows {
		if f.Merged() && rows[i].Merges() && !Newer(top, &rows[i]) {
			shown[rows[i].Group()] = true
		}
	}
	for i := range rows {
		switch {
		case !Newer(top, &rows[i]):
		case !f.Merged() || !rows[i].Merges():
			n++
		case !shown[rows[i].Group()] && !groups[rows[i].Group()]:
			groups[rows[i].Group()] = true
			n++
		}
	}
	return n
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
