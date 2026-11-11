package build

import (
	"slices"
	"testing"
	"time"

	"github.com/Zeus-Development/proxy-index/api/internal/store"
)

// outage is a two-target outage of acme in MK, newest first: down on
// generic_http (id 1) and generic_https (id 3) in the 10:00 slot, a minute
// apart, with a down in DE on generic_http alone (id 2) between them and a
// pulse of each target (ids 4 and 5, routine: never merged); then recovered
// on generic_https (id 6) and generic_http (id 7) in the 10:30 slot.
func outage() []store.Event {
	base := time.Date(2026, 11, 9, 10, 0, 0, 0, time.UTC)
	slot := func(m int) *time.Time { t := base.Add(time.Duration(m) * time.Minute); return &t }
	ev := func(id int64, m int, kind, country, target string, s *time.Time, signal int16) store.Event {
		return store.Event{ID: id, At: base.Add(time.Duration(m) * time.Minute), Kind: kind, Provider: sp("acme"),
			Product: sp("residential"), Country: sp(country), Target: sp(target), Signal: signal, Severity: "major", Slot: s}
	}
	rows := []store.Event{
		ev(1, 5, "down", "MK", "generic_http", slot(0), -1), ev(2, 5, "down", "DE", "generic_http", slot(0), -1),
		ev(3, 6, "down", "MK", "generic_https", slot(0), -1), ev(4, 6, "pulse_finished", "MK", "generic_http", slot(0), 0),
		ev(5, 6, "pulse_finished", "MK", "generic_https", slot(0), 0), ev(6, 35, "recovered", "MK", "generic_https", slot(30), 1),
		ev(7, 36, "recovered", "MK", "generic_http", slot(30), 1),
	}
	slices.SortFunc(rows, func(a, b store.Event) int {
		if store.Newer(&a, &b) {
			return -1
		}
		return 1
	})
	return rows
}

func outageWindow() EventWindow {
	return newEventWindow(&store.Inputs{Events: outage(), EventsComplete: true}, acmeText)
}

func answerOf(t *testing.T, w *EventWindow, f store.EventFilter) *EventsAnswer {
	t.Helper()
	a, ok := w.Answer(f)
	if !ok {
		t.Fatalf("a complete window asked the database for %+v", f)
	}
	return a
}

func targetsOfRows(rows []EventRow) [][]string {
	out := [][]string{}
	for _, r := range rows {
		out = append(out, r.Targets)
	}
	return out
}

var both = []string{"generic_http", "generic_https"}

// The incident tab gives one row per incident (EVENTS-MERGE-1): the MK
// outage's down and recovered once each, with both targets, as their
// generic_http row with the group's largest id; the DE down, seen on one
// target, is unchanged but for its targets. target= gives that target's own
// rows. The database fallback's rows give the same answer.
func TestAnOutageOnTwoTargetsIsOneRowPerEvent(t *testing.T) {
	w := outageWindow()
	f := store.EventFilter{Category: "incident", Limit: 100}
	a := answerOf(t, &w, f)
	if got := eventIDs(a); !slices.Equal(got, []int64{7, 3, 2}) {
		t.Fatalf("ids %v", got)
	}
	if got := targetsOfRows(a.Events); !slices.Equal(got[0], both) || !slices.Equal(got[1], both) || !slices.Equal(got[2], []string{"generic_http"}) {
		t.Fatalf("targets %v", got)
	}
	recovered, down := a.Events[0], a.Events[1]
	if *recovered.Target != "generic_http" || recovered.Title != "Recovered in MK" || *down.Target != "generic_http" ||
		down.At != store.PyTime(time.Date(2026, 11, 9, 10, 5, 0, 0, time.UTC)) || down.Kind != "down" || down.Category != "incident" {
		t.Fatalf("rows %+v", a.Events[:2])
	}
	https := answerOf(t, &w, store.EventFilter{Category: "incident", Target: "generic_https", Limit: 100})
	if got := eventIDs(https); !slices.Equal(got, []int64{6, 3}) || !slices.Equal(https.Events[0].Targets, []string{"generic_https"}) {
		t.Fatalf("target=generic_https: %v %v", got, targetsOfRows(https.Events))
	}
	if db := w.FromRows(f, outage()); !slices.Equal(eventIDs(db), []int64{7, 3, 2}) || !slices.Equal(db.Events[1].Targets, both) {
		t.Fatalf("database fallback %v %v", eventIDs(db), targetsOfRows(db.Events))
	}
	// Routine rows stay one per target.
	if p := answerOf(t, &w, store.EventFilter{Kinds: []string{"pulse_finished"}, Limit: 100}); !slices.Equal(eventIDs(p), []int64{5, 4}) {
		t.Fatalf("pulses %v", eventIDs(p))
	}
}

// A page boundary never splits a group: limit=1 newest first gives the
// recovered row whole, and walking after with limit=1 gives each incident
// once, whole, though the DE down's id falls between the MK down's two.
func TestAPageNeverSplitsAGroup(t *testing.T) {
	w := outageWindow()
	if a := answerOf(t, &w, store.EventFilter{Category: "incident", Limit: 1}); !slices.Equal(eventIDs(a), []int64{7}) ||
		!slices.Equal(a.Events[0].Targets, both) || a.More {
		t.Fatalf("newest: %v %v more %v", eventIDs(a), targetsOfRows(a.Events), a.More)
	}
	after, ids, targets := int64(0), []int64{}, [][]string{}
	for more, reads := true, 0; more; reads++ {
		if reads > 5 {
			t.Fatal("the cursor does not advance")
		}
		a := answerOf(t, &w, store.EventFilter{Category: "incident", After: &after, Limit: 1})
		ids, targets = append(ids, eventIDs(a)...), append(targets, targetsOfRows(a.Events)...)
		after, more = *a.Cursor, a.More
	}
	if !slices.Equal(ids, []int64{2, 3, 7}) || !slices.Equal(targets[1], both) || !slices.Equal(targets[2], both) {
		t.Fatalf("walk %v %v", ids, targets)
	}
	// The database fallback's read past after=2 with the MK down's row below
	// it (store.QueryEvents' groupsBelow) gives that down whole too.
	rows := slices.DeleteFunc(outage(), func(e store.Event) bool { return e.ID == 2 })
	after = 2
	if a := w.FromRows(store.EventFilter{Category: "incident", After: &after, Limit: 1}, rows); !slices.Equal(eventIDs(a), []int64{3}) ||
		!slices.Equal(a.Events[0].Targets, both) || *a.Events[0].Target != "generic_http" {
		t.Fatalf("fallback page %v %v", eventIDs(a), targetsOfRows(a.Events))
	}
}

// The dataset's tape, which the home and network tapes are cut from, holds
// one row per incident, from the window and from the tab heads alike.
func TestTheDatasetTapeHoldsOneRowPerIncident(t *testing.T) {
	rows := outage()
	w := newEventWindow(&store.Inputs{Events: rows[:3], EventsComplete: true}, acmeText)
	kinds := map[string]int{}
	for _, e := range w.measuredTape(rows[3:]) {
		kinds[e.Kind]++
	}
	if kinds["down"] != 2 || kinds["recovered"] != 1 || kinds["pulse_finished"] != 2 || len(kinds) != 3 {
		t.Fatalf("tape kinds %v", kinds)
	}
}
