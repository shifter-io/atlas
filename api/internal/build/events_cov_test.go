package build

import (
	"testing"
	"time"

	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/Zeus-Development/proxy-index/api/internal/store"
)

var windowBase = time.Date(2026, 11, 3, 4, 0, 0, 0, time.UTC)

func windowEvent(id int64, minutesAgo int, kind, provider string) store.Event {
	return store.Event{ID: id, At: windowBase.Add(-time.Duration(minutesAgo) * time.Minute), Kind: kind, Provider: sp(provider)}
}

// eventWindow holds three window rows (two hour releases of network a, an
// outage of b) and two older notable rows of a, neither list complete.
func eventWindow() EventWindow {
	raw := []store.Event{
		windowEvent(6, 0, "estimate_hour_released", "a"),
		windowEvent(5, 1, "pulse_down", "b"),
		windowEvent(4, 2, "estimate_hour_released", "a"),
	}
	notable := []store.Event{windowEvent(3, 3, "latency_rise", "a"), windowEvent(2, 4, "pulse_recovered", "a")}
	return newEventWindow(&store.Inputs{Events: raw, Notable: notable})
}

func eventIDs(rows []store.EventJSON) []int64 {
	out := []int64{}
	for _, r := range rows {
		out = append(out, r.ID)
	}
	return out
}

func sameIDs(got []store.EventJSON, want ...int64) bool {
	ids := eventIDs(got)
	if len(ids) != len(want) {
		return false
	}
	for i := range want {
		if ids[i] != want[i] {
			return false
		}
	}
	return true
}

// A filter naming kinds gets the newest matching window rows, answered from
// memory only when the limit is reached, the window is complete or it reaches
// back to the filter's since.
func TestAnswerWithKindsFromTheWindow(t *testing.T) {
	w := eventWindow()
	hours := []string{"estimate_hour_released"}
	if out, ok := w.Answer(store.EventFilter{Kinds: hours, Limit: 1}); !ok || !sameIDs(out, 6) {
		t.Errorf("limit reached: %v, %v", eventIDs(out), ok)
	}
	if out, ok := w.Answer(store.EventFilter{Kinds: hours, Limit: 5}); ok || !sameIDs(out, 6, 4) {
		t.Errorf("incomplete window: %v, %v", eventIDs(out), ok)
	}
	since := windowBase.Add(-2 * time.Minute)
	if out, ok := w.Answer(store.EventFilter{Kinds: hours, Since: &since, Limit: 5}); !ok || !sameIDs(out, 6) {
		t.Errorf("since covered: %v, %v", eventIDs(out), ok)
	}
	w.Complete = true
	if out, ok := w.Answer(store.EventFilter{Kinds: hours, Limit: 5}); !ok || !sameIDs(out, 6, 4) {
		t.Errorf("complete window: %v, %v", eventIDs(out), ok)
	}
}

// A prioritized filter (no kinds, no tab) takes notable rows from past the
// window too, and is answered from memory only when the rows held contain its
// newest rows and its newest notable ones.
func TestAnswerPrioritizedFromTheWindowAndNotableRows(t *testing.T) {
	w := eventWindow()
	// Network a: two window rows (enough for 2) and two notable rows (quota 1):
	// the newest notable row, then the newest remaining one.
	if out, ok := w.Answer(store.EventFilter{Product: "a", Limit: 2}); !ok || !sameIDs(out, 6, 3) {
		t.Errorf("limit 2: %v, %v", eventIDs(out), ok)
	}
	if out, ok := w.Answer(store.EventFilter{Product: "a", Limit: 6}); ok || out != nil {
		t.Errorf("too few window rows: %v, %v", eventIDs(out), ok)
	}
	w.Complete = true
	if out, ok := w.Answer(store.EventFilter{Product: "a", Limit: 6}); !ok || !sameIDs(out, 6, 4, 3, 2) {
		t.Errorf("complete: %v, %v", eventIDs(out), ok)
	}
	// Only routine rows held and the notable rows incomplete: a notable row may
	// be older than them.
	routine := newEventWindow(&store.Inputs{Events: []store.Event{windowEvent(6, 0, "estimate_hour_released", "a")}})
	if out, ok := routine.Answer(store.EventFilter{Product: "a", Limit: 1}); ok || out != nil {
		t.Errorf("notable rows missing: %v, %v", eventIDs(out), ok)
	}
	// Nothing newer than since, and the window reaches back to it: no rows.
	since := windowBase
	if out, ok := routine.Answer(store.EventFilter{Product: "a", Since: &since, Limit: 1}); !ok || !sameIDs(out) {
		t.Errorf("since covered: %v, %v", eventIDs(out), ok)
	}
}

// WithEvents swaps in the new events and their fingerprint and shares the rest.
func TestWithEventsReplacesOnlyTheEvents(t *testing.T) {
	before, after := int64(4), int64(9)
	ds := &model.Dataset{GeneratedAt: "2026-11-03T04:00:00.000Z"}
	res := &Result{Fingerprint: store.Fingerprint{MaxEventID: &before}, Dataset: ds}
	in := &store.Inputs{Fingerprint: store.Fingerprint{MaxEventID: &after}, EventsComplete: true,
		Events: []store.Event{windowEvent(9, 0, "pulse_down", "a")}}
	out := WithEvents(res, in)
	if out == res || out.Dataset != ds || *out.Fingerprint.MaxEventID != 9 || !out.Events.Complete || !sameIDs(out.Events.JSON, 9) {
		t.Fatalf("with events: %+v", out)
	}
	if *res.Fingerprint.MaxEventID != 4 || res.Events.Raw != nil {
		t.Fatalf("the original changed: %+v", res)
	}
}
