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
		read           store.EventsRead
		bound          int64
		open           int
		known, pending bool
	}{
		{store.EventsRead{MaxID: 10, Xmin: 100, Next: 105}, 0, 1, false, true},
		{store.EventsRead{MaxID: 12, Xmin: 103, Next: 110}, 0, 2, false, true},  // 100..104 may still run
		{store.EventsRead{MaxID: 15, Xmin: 105, Next: 112}, 10, 2, true, true},  // every xid of the first read ended
		{store.EventsRead{MaxID: 15, Xmin: 112, Next: 112}, 15, 0, true, false}, // no xid handed out is running
		{store.EventsRead{MaxID: 14, Xmin: 113, Next: 113}, 15, 0, true, false}, // a deleted newest row does not lower it
	}
	m := cursorMarks{}
	for i, s := range steps {
		m = m.next(s.read)
		w := EventWindow{marks: m}
		if m.bound != s.bound || len(m.open) != s.open || m.known != s.known || w.Pending() != s.pending {
			t.Fatalf("read %d: bound %d, %d open, known %v, pending %v; want %d, %d, %v, %v", i, m.bound, len(m.open), m.known,
				w.Pending(), s.bound, s.open, s.known, s.pending)
		}
	}
	if m := m.next(store.EventsRead{MaxID: 20}); m.bound != math.MaxInt64 || (&EventWindow{marks: m}).Pending() {
		t.Fatalf("a read without a snapshot: bound %d", m.bound)
	}
	if m := (cursorMarks{bound: math.MaxInt64}).next(store.EventsRead{MaxID: 3, Xmin: 7, Next: 9}); m.bound != 0 {
		t.Fatalf("a snapshot after none starts from 0: %d", m.bound)
	}
	// A transaction open over many reads keeps the oldest and the newest.
	m = cursorMarks{}
	for i := int64(0); i < 40; i++ {
		m = m.next(store.EventsRead{MaxID: i + 1, Xmin: 50, Next: 100 + i})
	}
	if len(m.open) != openReads || m.open[0].MaxID != 1 || m.open[openReads-1].MaxID != 40 {
		t.Fatalf("%d open reads: %+v", len(m.open), m.open)
	}
	if m = m.next(store.EventsRead{MaxID: 41, Xmin: 140, Next: 141}); m.bound != 40 || len(m.open) != 1 {
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
	read := store.EventsRead{MaxID: 10, Xmin: 100, Next: 104}
	w := newEventWindow(&store.Inputs{Events: rows, EventsComplete: true, EventsRead: read}, acmeText)
	w.marks.bound, w.marks.known = 6, true
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
	// Before any bound is known (a first window after a restart), the
	// default poll's cursor stops below its smallest row, and after it the
	// bound (0) still holds every row back.
	first := newEventWindow(&store.Inputs{Events: rows[:6], NotableComplete: true, LateComplete: true, EventsRead: read}, acmeText)
	if a, ok := first.Answer(store.EventFilter{Limit: 6}); !ok || len(a.Events) != 6 || *a.Cursor != 4 || !a.More || !first.Pending() || first.marks.known {
		t.Fatalf("the default poll of a first window: %v cursor %d more %v", eventIDs(a), *a.Cursor, a.More)
	}
	if a, ok := first.Answer(store.EventFilter{After: sp64(4), Limit: 100}); !ok || len(a.Events) != 0 || !a.More {
		t.Fatalf("after the first window's cursor: %v more %v", eventIDs(a), a.More)
	}
	prev := &Result{Events: newEventWindow(&store.Inputs{EventsRead: store.EventsRead{MaxID: 8, Xmin: 90, Next: 95}}, acmeText)}
	res := &Result{Events: newEventWindow(&store.Inputs{EventsRead: store.EventsRead{MaxID: 10, Xmin: 96, Next: 99}}, acmeText)}
	if FollowEvents(res, prev); res.Events.marks.bound != 8 || !res.Events.Pending() {
		t.Fatalf("a full build after a read: bound %d", res.Events.marks.bound)
	}
}
