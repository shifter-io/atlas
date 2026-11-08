package build

import (
	"math"
	"slices"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/store"
)

// EV-CURSOR-SETTLE-1: the cursor bound is the largest id of a read whose
// xmax a later read's xmin reached (or of a read with nothing running), it
// never goes down, and a read without a snapshot bounds nothing.
func TestCursorMarksPassOnlyReadsEveryTransactionOfWhichEnded(t *testing.T) {
	steps := []struct {
		read    store.EventsRead
		bound   int64
		open    int
		pending bool
	}{
		{store.EventsRead{MaxID: 10, Xmin: 100, Xmax: 105}, 0, 1, true},
		{store.EventsRead{MaxID: 12, Xmin: 103, Xmax: 110}, 0, 2, true},   // 100..104 still running
		{store.EventsRead{MaxID: 15, Xmin: 105, Xmax: 112}, 10, 2, true},  // the first read's transactions ended
		{store.EventsRead{MaxID: 15, Xmin: 112, Xmax: 112}, 15, 0, false}, // nothing running
		{store.EventsRead{MaxID: 14, Xmin: 113, Xmax: 113}, 15, 0, false}, // a deleted newest row does not lower it
	}
	m := cursorMarks{}
	for i, s := range steps {
		m = m.next(s.read)
		w := EventWindow{marks: m}
		if m.bound != s.bound || len(m.open) != s.open || w.Pending() != s.pending {
			t.Fatalf("read %d: bound %d, %d open, pending %v; want %d, %d, %v", i, m.bound, len(m.open), w.Pending(), s.bound, s.open, s.pending)
		}
	}
	if m := m.next(store.EventsRead{MaxID: 20}); m.bound != math.MaxInt64 || (&EventWindow{marks: m}).Pending() {
		t.Fatalf("a read without a snapshot: bound %d", m.bound)
	}
	if m := (cursorMarks{bound: math.MaxInt64}).next(store.EventsRead{MaxID: 3, Xmin: 7, Xmax: 9}); m.bound != 0 {
		t.Fatalf("a snapshot after none starts from 0: %d", m.bound)
	}
	// A transaction open over many reads keeps the oldest and the newest.
	m = cursorMarks{}
	for i := int64(0); i < 40; i++ {
		m = m.next(store.EventsRead{MaxID: i + 1, Xmin: 50, Xmax: 100 + i})
	}
	if len(m.open) != openReads || m.open[0].MaxID != 1 || m.open[openReads-1].MaxID != 40 {
		t.Fatalf("%d open reads: %+v", len(m.open), m.open)
	}
	if m = m.next(store.EventsRead{MaxID: 41, Xmin: 140, Xmax: 141}); m.bound != 40 || len(m.open) != 1 {
		t.Fatalf("once it ends: bound %d, %d open", m.bound, len(m.open))
	}
}

// An after answer passes no id above the bound, from memory and from the
// fallback alike (more stays true); the default poll returns its rows but
// caps its cursor at the bound, so a poll with that cursor gets the rows
// above it (again: clients deduplicate by id) and loses none. A full build
// keeps the marks of the one before it.
func TestTheCursorStopsAtTheBound(t *testing.T) {
	rows := burst(10)
	read := store.EventsRead{MaxID: 10, Xmin: 100, Xmax: 104}
	w := newEventWindow(&store.Inputs{Events: rows, EventsComplete: true, EventsRead: read}, acmeText)
	w.marks.bound = 6
	check := func(name string, f store.EventFilter, ids []int64, cursor int64, more bool) {
		t.Helper()
		memory, ok := w.Answer(f)
		for src, a := range map[string]*EventsAnswer{"memory": memory, "database": w.FromRows(f, dbAfter(rows, f))} {
			if !ok || !slices.Equal(eventIDs(a), ids) || *a.Cursor != cursor || a.More != more {
				t.Errorf("%s from %s: %v cursor %d more %v, want %v cursor %d more %v", name, src, eventIDs(a), *a.Cursor, a.More, ids, cursor, more)
			}
		}
	}
	check("after 0", store.EventFilter{After: sp64(0), Limit: 100}, []int64{1, 2, 3, 4, 5, 6}, 6, true)
	check("after 0, limit 2", store.EventFilter{After: sp64(0), Limit: 2}, []int64{1, 2}, 2, true)
	check("at the bound", store.EventFilter{After: sp64(6), Limit: 100}, []int64{}, 6, true)
	check("past the bound", store.EventFilter{After: sp64(8), Limit: 100}, []int64{}, 8, true)
	check("US rows (odd ids)", store.EventFilter{After: sp64(0), Country: "US", Limit: 100}, []int64{1, 3, 5}, 5, true)
	def, _ := w.Answer(store.EventFilter{Limit: 100})
	if len(def.Events) != 10 || *def.Cursor != 6 || !def.More {
		t.Fatalf("the default poll: %v cursor %d more %v", eventIDs(def), *def.Cursor, def.More)
	}
	w.marks.bound = 10
	if a, _ := w.Answer(store.EventFilter{After: def.Cursor, Limit: 100}); !slices.Equal(eventIDs(a), []int64{7, 8, 9, 10}) || a.More {
		t.Fatalf("the hand-off from the default poll: %v more %v", eventIDs(a), a.More)
	}
	prev := &Result{Events: newEventWindow(&store.Inputs{EventsRead: store.EventsRead{MaxID: 8, Xmin: 90, Xmax: 95}}, acmeText)}
	res := &Result{Events: newEventWindow(&store.Inputs{EventsRead: store.EventsRead{MaxID: 10, Xmin: 96, Xmax: 99}}, acmeText)}
	if FollowEvents(res, prev); res.Events.marks.bound != 8 || !res.Events.Pending() {
		t.Fatalf("a full build after a read: bound %d", res.Events.marks.bound)
	}
}
