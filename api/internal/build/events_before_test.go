package build

import (
	"fmt"
	"slices"
	"testing"
	"time"

	"github.com/Zeus-Development/atlas/api/internal/store"
)

var historyStart = time.Date(2026, 11, 10, 8, 0, 0, 0, time.UTC)

// history is six pulse slots of acme (residential) and other (datacenter),
// newest first, ids in write order. Each slot has a census of both at one
// time (no target, never merged), each target's pulse in US and DE at one
// time, a two-target down of acme in DE whose generic_https row has the
// group's largest id but stands below its generic_http row, with a
// success_drop of other between them, a two-target recovered of acme in US
// at one time, and two no_node rows of other at that time too.
func history() []store.Event {
	rows, id := []store.Event{}, int64(0)
	add := func(m int, kind, provider, country, target string, slot int) {
		id++
		e := store.Event{ID: id, At: historyStart.Add(time.Duration(m) * time.Minute), Kind: kind, Provider: sp(provider),
			Product: sp("residential"), Country: sp(country), Severity: "info"}
		if provider == "other" {
			e.Product = sp("datacenter")
		}
		if target != "" {
			e.Target = sp(target)
		}
		if slot >= 0 {
			s := historyStart.Add(time.Duration(slot*30) * time.Minute)
			e.Slot = &s
		}
		rows = append(rows, e)
	}
	for s := range 6 {
		m := s * 30
		add(m+1, "census_finished", "acme", "US", "", -1)
		add(m+1, "census_finished", "other", "DE", "", -1)
		for _, c := range []string{"US", "DE"} {
			add(m+6, "pulse_finished", "acme", c, "generic_http", s)
			add(m+6, "pulse_finished", "acme", c, "generic_https", s)
		}
		add(m+9, "down", "acme", "DE", "generic_http", s)
		add(m+8, "success_drop", "other", "US", "generic_http", s)
		add(m+7, "down", "acme", "DE", "generic_https", s)
		add(m+12, "recovered", "acme", "US", "generic_https", s)
		add(m+12, "recovered", "acme", "US", "generic_http", s)
		add(m+12, "no_node", "other", "DE", "", -1)
		add(m+12, "no_node", "other", "DE", "", -1)
	}
	slices.SortFunc(rows, func(a, b store.Event) int {
		if store.Newer(&a, &b) {
			return -1
		}
		return 1
	})
	return rows
}

// historyFilters are the filters before combines with, by name.
func historyFilters() map[string]store.EventFilter {
	since := historyStart.Add(35 * time.Minute)
	return map[string]store.EventFilter{
		"none": {}, "network": {Provider: "acme"}, "product": {Product: "other/datacenter"}, "event": {Events: []string{"down", "pulse_finished"}},
		"kind": {Groups: []string{"incident"}}, "country": {Country: "DE"}, "target": {Target: "generic_https"}, "q": {Words: []string{"de"}},
		"proxyKind": {ProxyKind: "residential"}, "since": {Since: &since}, "since and kind": {Since: &since, Groups: []string{"pulse"}},
	}
}

// historyAnswer answers f the ways /v1/events does: from a complete window,
// from a window of the newest 30 rows and the non-routine rest (answering
// what it provably holds, else the fallback), and from the fallback's rows
// (store.QueryEvents' superset: every row, the anchor's included).
func historyAnswer(t *testing.T, how string, f store.EventFilter) *EventsAnswer {
	t.Helper()
	rows := history()
	switch how {
	case "complete":
		w := newEventWindow(&store.Inputs{Events: rows, EventsComplete: true}, acmeText)
		return answerOf(t, &w, f)
	case "window":
		notable := slices.DeleteFunc(slices.Clone(rows[30:]), func(e store.Event) bool { return store.Routine(e.Kind) })
		w := newEventWindow(&store.Inputs{Events: rows[:30], Notable: notable, NotableComplete: true}, acmeText)
		if a, ok := w.Answer(f); ok {
			return a
		}
		return w.FromRows(f, rows)
	}
	w := newEventWindow(&store.Inputs{Events: rows[:1], EventsComplete: false}, acmeText)
	return w.FromRows(f, rows)
}

// Paging back with before (EVENTS-BEFORE-1) from the newest row gives every
// later row of the full answer exactly once, in order, whole (a merged row
// with all its targets), with every filter, page size and source; more is
// true on every page but the last, and a page before the last row is empty.
// The down whose largest id stands below its newest row is placed where that
// row stands, so the success_drop between them is not skipped.
func TestBeforePagesEveryRowOnce(t *testing.T) {
	for name, f := range historyFilters() {
		for _, how := range []string{"complete", "window", "fallback"} {
			for _, limit := range []int{1, 2, 3, 5} {
				t.Run(fmt.Sprintf("%s/%s/%d", name, how, limit), func(t *testing.T) {
					all := f
					all.Limit = 500
					full := historyAnswer(t, "complete", all).Events
					if len(full) < 3*limit+2 {
						t.Fatalf("%d rows, fewer than 3 pages", len(full))
					}
					got, page := []EventRow{}, f
					page.Limit, page.Before = limit, &full[0].ID
					for reads := 0; ; reads++ {
						if reads > len(full) {
							t.Fatal("before does not move")
						}
						a := historyAnswer(t, how, page)
						got = append(got, a.Events...)
						if !a.More {
							break
						}
						page.Before = &a.Events[len(a.Events)-1].ID
					}
					if !slices.EqualFunc(got, full[1:], func(a, b EventRow) bool { return a.ID == b.ID && slices.Equal(a.Targets, b.Targets) }) {
						t.Fatalf("pages %v, want %v", rowIDs(got), rowIDs(full[1:]))
					}
					page.Before = &full[len(full)-1].ID
					if a := historyAnswer(t, how, page); len(a.Events) != 0 || a.More {
						t.Fatalf("before the last row: %v more %v", rowIDs(a.Events), a.More)
					}
				})
			}
		}
	}
	// The newest slot's down: ids 72 (generic_http, 10:39) and 74 (generic_https,
	// 10:37), with the success_drop 73 (10:38) between them.
	down := int64(74)
	a := historyAnswer(t, "complete", store.EventFilter{Groups: []string{"incident"}, Before: &down, Limit: 1})
	if !slices.Equal(eventIDs(a), []int64{73}) {
		t.Fatalf("before the down: %v", eventIDs(a))
	}
}

func rowIDs(rows []EventRow) []int64 {
	out := []int64{}
	for _, r := range rows {
		out = append(out, r.ID)
	}
	return out
}

// more is true when older rows match below a newest-first page, from memory
// and from the fallback: the first page (prioritized or not) and a since
// page; false when the page holds every match.
func TestMoreTellsOlderRows(t *testing.T) {
	since := historyStart.Add(35 * time.Minute)
	for _, how := range []string{"complete", "window", "fallback"} {
		for _, f := range []store.EventFilter{{Limit: 5}, {Groups: []string{"incident"}, Limit: 3}, {Since: &since, Limit: 4}, {Country: "DE", Limit: 2}} {
			if a := historyAnswer(t, how, f); len(a.Events) != f.Limit || !a.More {
				t.Errorf("%s %+v: %v more %v", how, f, eventIDs(a), a.More)
			}
		}
		if a := historyAnswer(t, how, store.EventFilter{Limit: 500}); len(a.Events) != 66 || a.More {
			t.Errorf("%s, every row: %d rows more %v", how, len(a.Events), a.More)
		}
	}
}

// A window that does not hold the before row asks the database, which places
// it or answers store.ErrUnknownCursor; so does one that cannot prove the page
// and the row after it.
func TestBeforeAsksTheDatabaseOutsideTheWindow(t *testing.T) {
	rows := history()
	w := newEventWindow(&store.Inputs{Events: rows[:30]}, acmeText)
	old, last := rows[60].ID, rows[29].ID
	for _, f := range []store.EventFilter{{Before: &old, Limit: 2}, {Before: &last, Limit: 2}} {
		if _, ok := w.Answer(f); ok {
			t.Errorf("before %d answered from memory", *f.Before)
		}
	}
}
