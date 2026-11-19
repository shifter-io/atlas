package build

import (
	"encoding/json"
	"fmt"
	"slices"
	"testing"
	"time"

	"github.com/shifter-io/atlas/api/internal/model"
	"github.com/shifter-io/atlas/api/internal/snap"
	"github.com/shifter-io/atlas/api/internal/store"
	"github.com/shifter-io/atlas/api/internal/testscale"
)

func tapeIDs(rows []model.TapeEvent) []string {
	out := []string{}
	for _, r := range rows {
		out = append(out, r.ID)
	}
	return out
}

// tapeKinds counts rows by kind and reports whether they are newest first.
func tapeKinds(rows []model.TapeEvent) (map[string]int, bool) {
	kinds, ordered := map[string]int{}, true
	for i, r := range rows {
		kinds[r.Kind]++
		if i > 0 && snap.Ms(model.SV(r.At)) > snap.Ms(model.SV(rows[i-1].At)) {
			ordered = false
		}
	}
	return kinds, ordered
}

// kindEvents is n events of kind from id down, a minute apart back from at.
func kindEvents(kind string, id int64, at time.Time, n int) []store.Event {
	out := []store.Event{}
	for i := range n {
		provider := fmt.Sprintf("net%02d", i%3)
		out = append(out, store.Event{ID: id - int64(i), At: at.Add(-time.Duration(i) * time.Minute), Kind: kind,
			Provider: &provider, Product: sp("residential"), Severity: "info", Data: json.RawMessage(`{}`)})
	}
	return out
}

// The home tape is the newest 10 rows of each tab, merged newest first: 30
// census rows newer than the others leave 10, a tab with 3 rows keeps them, a
// tab without rows has none, and a row of no tab is left out (HOME-TAPE-1).
func TestHomeTapeTakesEachTabsNewestRows(t *testing.T) {
	t.Parallel()
	at := time.Date(2026, 11, 10, 12, 0, 0, 0, time.UTC)
	events := kindEvents("estimate_hour_released", 100, at, 30)
	events = append(events, kindEvents("pulse_finished", 70, at.Add(-time.Hour), 12)...)
	events = append(events, kindEvents("latency_rise", 50, at.Add(-2*time.Hour), 3)...)
	js := []store.EventJSON{}
	for i := range events {
		js = append(js, events[i].JSON())
	}
	rows := TapeFromEvents(js, nil, "", nil)
	rows = append(rows, model.TapeEvent{ID: "editorial", Kind: "note", At: rows[0].At})
	got := tabHeads(rows)
	kinds, ordered := tapeKinds(got)
	if len(got) != 23 || kinds["census"] != 10 || kinds["pulse"] != 10 || kinds["incident"] != 3 || len(kinds) != 3 || !ordered {
		t.Fatalf("%d rows %v, newest first %v", len(got), kinds, ordered)
	}
	if got[0].ID != "event-100" || got[10].ID != "event-70" || got[22].ID != "event-48" {
		t.Errorf("rows %v", tapeIDs(got))
	}
	if tabHeads(nil) == nil || len(tabHeads(nil)) != 0 {
		t.Error("no rows is an empty tape")
	}
}

// A build whose newest 30 events are census, with the older pulse and
// incident events only among the tab heads the store read
// (store.Inputs.TapeHeads), puts both in the dataset's tape and every
// range's home tape, newest first, 10 at most of each; the audit tab, with
// no event, stays empty.
func TestHomeTapeHasEveryTabWithEvents(t *testing.T) {
	t.Parallel()
	in, err := testscale.Inputs(testscale.Shape{Networks: 3, Countries: 1, Exits: 3, Tail: 5, Unmeasured: 1})
	if err != nil {
		t.Fatal(err)
	}
	at := time.Date(2026, 11, 3, 4, 0, 0, 0, time.UTC)
	in.Events, in.EventsComplete = kindEvents("daily_test_finished", 100, at, 30), false
	in.TapeHeads = append(in.Events[:10:10], kindEvents("pulse_down", 60, at.Add(-time.Hour), 10)...)
	in.TapeHeads = append(in.TapeHeads, kindEvents("success_drop", 40, at.Add(-2*time.Hour), 4)...)
	res, err := Build(in)
	if err != nil {
		t.Fatal(err)
	}
	if kinds, _ := tapeKinds(res.Dataset.Tape); kinds["census"] != 30 || kinds["pulse"] != 10 || kinds["incident"] != 4 {
		t.Errorf("dataset tape %v", kinds)
	}
	for _, r := range Ranges {
		h := homeOf(t, res.Pages, r.ID)
		kinds, ordered := tapeKinds(h.Tape)
		if len(h.Tape) != 24 || kinds["census"] != 10 || kinds["pulse"] != 10 || kinds["incident"] != 4 || kinds["audit"] != 0 || !ordered {
			t.Errorf("%s: %d rows %v, newest first %v", r.ID, len(h.Tape), kinds, ordered)
		}
		if ids := tapeIDs(h.Tape); !slices.Contains(ids, "event-100") || !slices.Contains(ids, "event-37") {
			t.Errorf("%s: rows %v", r.ID, ids)
		}
	}
}
