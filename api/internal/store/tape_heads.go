package store

import (
	"cmp"
	"context"
	"fmt"
	"slices"
	"strings"
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
	RegisterStatement(Statement{"load.tape_heads.networks", "Inputs.loadTapeHeads", everyLayout, func(*ExplainParams) (string, []any) {
		return networkTapeHeadSQL, networkTapeHeadArgs(time.Now().AddDate(0, 0, -StatsDays))
	}})
}

// loadTapeHeads reads Inputs.TapeHeads in tx: each tab's newest TapeHeadRows
// events, so a burst of one tab (an hour's census releases) never leaves
// another tab of the home tape empty. One query per tab, each bounded below
// by tapeHeadStart, so a tab with few events never walks the whole table.
// Then each product's newest TapeHeadRows events of every tab
// (NETWORK-TAPE-QUOTA-1), for the network pages' tapes, without the rows
// already read. It reads after loadEventStats; on a framework schema without
// the event counts (EventStats.Missing) the queries are unbounded.
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
	from := time.Time{}
	if !in.EventStats.Missing {
		from = in.EventStats.End.AddDate(0, 0, -StatsDays)
	}
	rows, err := collect(ctx, tx, networkTapeHeadSQL, ScanEvent, networkTapeHeadArgs(from)...)
	if err != nil {
		return fmt.Errorf("tape heads of the networks: %w", err)
	}
	seen := map[int64]bool{}
	for _, e := range in.TapeHeads {
		seen[e.ID] = true
	}
	for _, e := range rows {
		if !seen[e.ID] {
			in.TapeHeads = append(in.TapeHeads, e)
		}
	}
	slices.SortFunc(in.TapeHeads, func(a, b Event) int { return cmp.Or(b.At.Compare(a.At), cmp.Compare(b.ID, a.ID)) })
	return nil
}

// tapeHeadStart is where tab's read starts: the newest 30-minute bucket of
// the event counts from which they hold TapeHeadRows of its merged rows, else the
// counts' first bucket (StatsDays before End); ok is false when they hold none
// of its events. With no counts (Missing) it is the zero time: no bound.
func tapeHeadStart(s *EventStats, tab string) (from time.Time, ok bool) {
	if s.Missing {
		return time.Time{}, true
	}
	byStart := map[time.Time]int{}
	for _, b := range s.Buckets {
		if b.Category == tab {
			byStart[b.Start] += b.Events // merged rows: the duplicates' entries are negative
		}
	}
	starts := make([]time.Time, 0, len(byStart))
	for t, n := range byStart {
		if n > 0 {
			starts = append(starts, t)
		}
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
// from (none: the zero time), and its arguments. A tab whose every row merges
// (incident, audit) reads GroupRows times as many, so its TapeHeadRows merged
// rows (EVENTS-MERGE-1) are read whole; the pulse tab is mostly routine rows.
func tapeHeadQuery(tab string, from time.Time) (string, []any) {
	f := EventFilter{Category: tab}
	clauses, args := eventClauses(f)
	if !from.IsZero() {
		args = append(args, from)
		clauses = append(clauses, "e.at>=$"+itoa(len(args)))
	}
	limit := TapeHeadRows
	if f.nonRoutine() {
		limit *= GroupRows
	}
	return eventsQuery(clauses, args, limit)
}

// networkTapeHeadSQL reads each product's newest $2 events of every tab from
// $1 (NETWORK-TAPE-QUOTA-1): a window partitioned by product and tab, so at
// most products × 4 tabs × $2 rows, $2 × GroupRows ($3) for the tabs whose
// rows merge (all but census), so each holds its $2 merged rows. The window
// ranks ids and times alone; only the rows kept are read whole.
var networkTapeHeadSQL = "SELECT " + strings.Replace(EventColumns, "FROM public.test_events e",
	`FROM (SELECT e.event_id, e.at, row_number() OVER (PARTITION BY e.product_id, `+categorySQL+`
 ORDER BY e.at DESC, e.event_id DESC) AS n, `+categorySQL+` AS category
 FROM public.test_events e WHERE e.product_id IS NOT NULL AND e.at >= $1) h
 JOIN public.test_events e ON e.event_id = h.event_id AND e.at = h.at`, 1) +
	" WHERE h.n <= CASE WHEN h.category = 'census' THEN $2::int ELSE $3::int END ORDER BY e.at DESC, e.event_id DESC"

// networkTapeHeadArgs is networkTapeHeadSQL's arguments for the bound from
// (the zero time: none).
func networkTapeHeadArgs(from time.Time) []any {
	return []any{from, TapeHeadRows, TapeHeadRows * GroupRows}
}
