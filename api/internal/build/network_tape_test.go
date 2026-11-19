package build

import (
	"encoding/json"
	"testing"
	"time"

	"github.com/shifter-io/atlas/api/internal/store"
	"github.com/shifter-io/atlas/api/internal/testscale"
)

// netEvents is n events of kind of provider's residential product from id
// down, a minute apart back from at.
func netEvents(provider, kind string, id int64, at time.Time, n int) []store.Event {
	out := []store.Event{}
	for i := range n {
		out = append(out, store.Event{ID: id - int64(i), At: at.Add(-time.Duration(i) * time.Minute), Kind: kind,
			Provider: &provider, Product: sp("residential"), Severity: "info", Data: json.RawMessage(`{}`)})
	}
	return out
}

// A network whose newest 13 events are all ip_overlap, with its older census,
// pulse and incident events only among the tape heads the store read
// (NETWORK-TAPE-QUOTA-1), has rows of every tab in its answer's tape, at
// most store.TapeHeadRows each, newest first, and no other network's rows; an
// editorial row of its own kind ("rotation") keeps its place; a network with
// incidents alone has those and empty other tabs.
func TestANetworkTapeHoldsEachTabsNewestRows(t *testing.T) {
	t.Parallel()
	in, err := testscale.Inputs(testscale.Shape{Networks: 3, Countries: 1, Exits: 3, Tail: 5, Unmeasured: 1})
	if err != nil {
		t.Fatal(err)
	}
	at := time.Date(2026, 11, 11, 14, 9, 0, 0, time.UTC)
	in.Events, in.EventsComplete = netEvents("net00", "ip_overlap", 200, at, 13), false
	in.TapeHeads = append(in.Events[:10:10], netEvents("net00", "daily_test_finished", 150, at.Add(-time.Hour), 15)...)
	in.TapeHeads = append(in.TapeHeads, netEvents("net00", "pulse_finished", 120, at.Add(-2*time.Hour), 12)...)
	in.TapeHeads = append(in.TapeHeads, netEvents("net00", "down", 100, at.Add(-3*time.Hour), 3)...)
	in.TapeHeads = append(in.TapeHeads, netEvents("net01", "down", 90, at.Add(-30*time.Minute), 5)...)
	in.Tape = append(in.Tape, store.TapeRow{ID: "t-rotation", Kind: "rotation", ProviderSlug: "net00", Product: "residential", OccurredAt: at.Add(time.Minute)})
	res, err := Build(in)
	if err != nil {
		t.Fatal(err)
	}
	ans := res.Pages.Network["net00/residential"]
	if ans == nil {
		t.Fatalf("no net00/residential answer")
	}
	kinds, ordered := tapeKinds(ans.Tape)
	if len(ans.Tape) != 34 || kinds["rotation"] != 1 || kinds["audit"] != 10 || kinds["census"] != 10 || kinds["pulse"] != 10 || kinds["incident"] != 3 || !ordered {
		t.Errorf("net00: %d rows %v, newest first %v", len(ans.Tape), kinds, ordered)
	}
	if ans.Tape[0].ID != "t-rotation" || ans.Tape[1].ID != "event-200" || ans.Tape[33].ID != "event-98" || ans.Records.Tape != 44 {
		t.Errorf("net00: rows %v, records %d", tapeIDs(ans.Tape), ans.Records.Tape)
	}
	for _, r := range ans.Tape {
		if r.ProviderSlug != "net00" {
			t.Errorf("net00's tape has %s's row %s", r.ProviderSlug, r.ID)
		}
	}
	other := res.Pages.Network["net01/residential"]
	if kinds, _ := tapeKinds(other.Tape); len(other.Tape) != 5 || kinds["incident"] != 5 {
		t.Errorf("net01: %d rows %v", len(other.Tape), kinds)
	}
}
