package build

import (
	"slices"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/store"
)

// EV-CURSOR-SETTLE-1: an after answer and its cursor stop before the first
// unsettled matching row, from memory and from the fallback alike, and more
// stays true; a filter it does not match, and the default poll, are not held.
func TestAfterStopsBeforeTheFirstUnsettledRow(t *testing.T) {
	rows := burst(10)           // newest first: rows[i] is id 10-i
	rows[10-4].Unsettled = true // id 4, of acme in DE
	rows[10-7].Unsettled = true // id 7
	w := newEventWindow(&store.Inputs{Events: rows, EventsComplete: true}, acmeText)
	if !w.Unsettled() || newEventWindow(&store.Inputs{Events: burst(10)}, acmeText).Unsettled() {
		t.Fatal("Unsettled does not report the window's unsettled rows")
	}
	check := func(name string, f store.EventFilter, ids []int64, cursor int64, more bool) {
		t.Helper()
		memory, ok := w.Answer(f)
		db := w.FromRows(f, dbAfter(rows, f))
		for src, a := range map[string]*EventsAnswer{"memory": memory, "database": db} {
			if !slices.Equal(eventIDs(a), ids) || *a.Cursor != cursor || a.More != more {
				t.Errorf("%s from %s: %v cursor %d more %v, want %v cursor %d more %v", name, src, eventIDs(a), *a.Cursor, a.More, ids, cursor, more)
			}
		}
		if !ok {
			t.Errorf("%s: a complete window asked the database", name)
		}
	}
	check("after 0", store.EventFilter{After: sp64(0), Limit: 100}, []int64{1, 2, 3}, 3, true)
	check("after 0, limit 2", store.EventFilter{After: sp64(0), Limit: 2}, []int64{1, 2}, 2, true)
	check("held at the first row", store.EventFilter{After: sp64(3), Limit: 100}, []int64{}, 3, true)
	check("after the first", store.EventFilter{After: sp64(4), Limit: 100}, []int64{5, 6}, 6, true)
	check("US rows (odd ids) pass id 4", store.EventFilter{After: sp64(0), Country: "US", Limit: 100}, []int64{1, 3, 5}, 5, true)
	if a, _ := w.Answer(store.EventFilter{Limit: 100}); len(a.Events) != 10 || *a.Cursor != 10 || a.More {
		t.Fatalf("the default poll changed: %v cursor %d more %v", eventIDs(a), *a.Cursor, a.More)
	}
	late := newEventWindow(&store.Inputs{Events: burst(10)[:5], Late: []store.Event{{ID: 11, Unsettled: true}}, LateComplete: true}, acmeText)
	if !late.Unsettled() {
		t.Fatal("an unsettled late row is not reported")
	}
}
