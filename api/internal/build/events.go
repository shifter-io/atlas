package build

import (
	"github.com/Zeus-Development/proxy-index-website/api/internal/store"
)

// EventWindow is one build's events in memory: the newest store.EventWindow rows
// of every kind (Raw, rendered once in JSON) and, past them, the newest
// non-routine rows (store.Inputs.Notable), so a burst of routine rows such as
// an hour's estimate releases never pushes a pulse down, a recovery or an
// incident out of memory, off the tape or off the first page of /v1/events.
type EventWindow struct {
	Raw      []store.Event
	JSON     []store.EventJSON
	Complete bool // Raw holds every row of test_events
	// Notable and NotableJSON are the non-routine rows older than Raw, newest
	// first; NotableComplete is true when they are all of them.
	Notable         []store.Event
	NotableJSON     []store.EventJSON
	NotableComplete bool
}

func newEventWindow(in *store.Inputs) EventWindow {
	render := func(rows []store.Event) []store.EventJSON {
		out := make([]store.EventJSON, len(rows))
		for i := range rows {
			out[i] = rows[i].JSON()
		}
		return out
	}
	return EventWindow{Raw: in.Events, JSON: render(in.Events), Complete: in.EventsComplete,
		Notable: in.Notable, NotableJSON: render(in.Notable), NotableComplete: in.NotableComplete}
}

// held is the number of rows in memory; row(i) is the window's rows, then the
// notable rows past them, newest first.
func (w *EventWindow) held() int { return len(w.Raw) + len(w.Notable) }

func (w *EventWindow) row(i int) (*store.Event, *store.EventJSON) {
	if i < len(w.Raw) {
		return &w.Raw[i], &w.JSON[i]
	}
	return &w.Notable[i-len(w.Raw)], &w.NotableJSON[i-len(w.Raw)]
}

// measuredTape is the measured rows of the dataset's tape: MeasuredTapeRows of
// the rows in memory, chosen by store.Prioritize, newest first.
func (w *EventWindow) measuredTape() []store.EventJSON {
	kind := func(i int) string { e, _ := w.row(i); return e.Kind }
	pick := store.Prioritize(w.held(), MeasuredTapeRows, kind)
	out := make([]store.EventJSON, 0, len(pick))
	for _, i := range pick {
		_, j := w.row(i)
		out = append(out, *j)
	}
	return out
}

// Answer answers a /v1/events filter from memory when the rows held provably
// contain its whole answer; ok=false means the database must be asked.
func (w *EventWindow) Answer(f store.EventFilter) (out []store.EventJSON, ok bool) {
	if f.Prioritized() {
		return w.prioritized(f)
	}
	out = []store.EventJSON{}
	for i := range w.Raw {
		if f.Match(&w.Raw[i]) {
			out = append(out, w.JSON[i])
			if len(out) == f.Limit {
				return out, true
			}
		}
	}
	return out, w.Complete || sinceCovers(f, w.Raw)
}

// prioritized is store.Pick from memory. It needs the newest f.Limit matching
// rows (from the window) and the newest store.NotableQuota non-routine ones
// (from the window, then the notable rows past it).
func (w *EventWindow) prioritized(f store.EventFilter) ([]store.EventJSON, bool) {
	matched := []int{}
	inWindow, notable := 0, 0
	for i := 0; i < w.held(); i++ {
		e, _ := w.row(i)
		if !f.Match(e) {
			continue
		}
		matched = append(matched, i)
		if i < len(w.Raw) {
			inWindow++
		}
		if !store.Routine(e.Kind) {
			notable++
		}
	}
	newest := w.Complete || inWindow >= f.Limit || sinceCovers(f, w.Raw)
	notableHeld := w.Complete || w.NotableComplete || notable >= store.NotableQuota(f.Limit) ||
		sinceCovers(f, w.Raw) || sinceCovers(f, w.Notable)
	if !newest || !notableHeld {
		return nil, false
	}
	kind := func(k int) string { e, _ := w.row(matched[k]); return e.Kind }
	out := []store.EventJSON{}
	for _, k := range store.Prioritize(len(matched), f.Limit, kind) {
		_, j := w.row(matched[k])
		out = append(out, *j)
	}
	return out, true
}

// sinceCovers reports whether rows (newest first) reach back to f.Since, so
// nothing older than them can match.
func sinceCovers(f store.EventFilter, rows []store.Event) bool {
	return f.Since != nil && len(rows) > 0 && !rows[len(rows)-1].At.After(*f.Since)
}
