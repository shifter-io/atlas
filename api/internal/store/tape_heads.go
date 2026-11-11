package store

import (
	"cmp"
	"context"
	"fmt"
	"slices"
	"time"

	"github.com/jackc/pgx/v5"
)

// TapeTabs is every live tape tab (Category), in the home tape's order.
var TapeTabs = []string{"census", "pulse", "incident", "audit"}

// TapeHeadRows is how many of each tab's newest events Inputs.TapeHeads holds:
// the home tape's rows per tab (HOME-TAPE-1).
const TapeHeadRows = 10

func init() {
	for _, tab := range TapeTabs {
		RegisterStatement(Statement{"load.tape_heads." + tab, "Inputs.loadTapeHeads", everyLayout, func(*ExplainParams) (string, []any) {
			// The slowest case: the tab's bound at the start of the 30 days.
			return tapeHeadQuery(tab, time.Now().AddDate(0, 0, -StatsDays))
		}})
	}
}

// loadTapeHeads reads Inputs.TapeHeads in tx: each tab's newest TapeHeadRows
// events, so a burst of one tab (an hour's census releases) never leaves
// another tab of the home tape empty. One query per tab, each bounded below
// by tapeHeadStart, so a tab with few events never walks the whole table. It
// reads after loadEventStats; on a framework schema without the event
// counts (EventStats.Missing) the queries are unbounded.
func (in *Inputs) loadTapeHeads(ctx context.Context, tx pgx.Tx) error {
	in.TapeHeads = []Event{}
	for _, tab := range TapeTabs {
		from, ok := tapeHeadStart(&in.EventStats, tab)
		if !ok {
			continue
		}
		sql, args := tapeHeadQuery(tab, from)
		rows, err := collect(ctx, tx, sql, ScanEvent, args...)
		if err != nil {
			return fmt.Errorf("tape heads %s: %w", tab, err)
		}
		in.TapeHeads = append(in.TapeHeads, rows...)
	}
	slices.SortFunc(in.TapeHeads, func(a, b Event) int { return cmp.Or(b.At.Compare(a.At), cmp.Compare(b.ID, a.ID)) })
	return nil
}

// tapeHeadStart is where tab's read starts: the newest 30-minute bucket of
// the event counts from which they hold TapeHeadRows of its events, else the
// counts' first bucket (StatsDays before End); ok is false when they hold none
// of its events. With no counts (Missing) it is the zero time: no bound.
func tapeHeadStart(s *EventStats, tab string) (from time.Time, ok bool) {
	if s.Missing {
		return time.Time{}, true
	}
	byStart := map[time.Time]int{}
	for _, b := range s.Buckets {
		if b.Category == tab && b.Events > 0 {
			byStart[b.Start] += b.Events
		}
	}
	starts := make([]time.Time, 0, len(byStart))
	for t := range byStart {
		starts = append(starts, t)
	}
	slices.SortFunc(starts, func(a, b time.Time) int { return b.Compare(a) })
	held := 0
	for _, t := range starts {
		if held += byStart[t]; held >= TapeHeadRows {
			return t, true
		}
	}
	return s.End.AddDate(0, 0, -StatsDays), len(starts) > 0
}

// tapeHeadQuery is the query of tab's newest TapeHeadRows events at or after
// from (none: the zero time), and its arguments.
func tapeHeadQuery(tab string, from time.Time) (string, []any) {
	clauses, args := eventClauses(EventFilter{Category: tab})
	if !from.IsZero() {
		args = append(args, from)
		clauses = append(clauses, "e.at>=$"+itoa(len(args)))
	}
	return eventsQuery(clauses, args, TapeHeadRows)
}
