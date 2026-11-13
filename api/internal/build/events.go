package build

import (
	"cmp"
	"slices"
	"strings"

	"github.com/Zeus-Development/proxy-index/api/internal/store"
)

// EventWindow is one build's events in memory: the newest store.EventWindow rows
// of every kind (Raw, rendered once in JSON) and, past them, the newest
// non-routine rows (store.Inputs.Notable), so a burst of routine rows such as
// an hour's estimate releases never pushes a pulse down, a recovery or an
// incident out of memory, off the tape or off the first page of /v1/events.
type EventWindow struct {
	Raw      []store.Event
	JSON     []EventRow
	Complete bool // Raw holds every row of test_events
	// Notable and NotableJSON are the non-routine rows older than Raw, newest
	// first; NotableComplete is true when they are all of them.
	Notable         []store.Event
	NotableJSON     []EventRow
	NotableComplete bool
	// Late and LateJSON are the rows with an id above Raw's smallest that are
	// older than Raw (store.Inputs.Late), so an after read sees them too.
	Late         []store.Event
	LateJSON     []EventRow
	LateComplete bool
	// Stats is the event counts read with the rows (store.Inputs.EventStats);
	// Held is the bodies of the registered answers with Events built from
	// them, by answer name (buildLive), and Unbuilt each one left out, with
	// why. live is what the next WithEvents builds them from.
	Stats   store.EventStats
	Held    map[string]map[string]any
	Unbuilt map[string]string
	live    *Context
	text    eventText
	marks   cursorMarks
}

// EventRow is one /v1/events row: the framework's TestEvent and the live
// tape's text and values (TapeFromEvents), rendered once when the row is
// loaded.
// ProviderSlug and ProviderName are null for a provider without a network.
// Targets is the targets of the rows merged into it (EVENTS-MERGE-1), sorted:
// its own target alone, or none, for a row standing for itself.
type EventRow struct {
	store.EventJSON
	Targets      []string    `json:"targets"`
	Title        string      `json:"-"` // Title and Detail: English text for q's search only (#962)
	Detail       string      `json:"-"`
	Values       EventValues `json:"values"`
	ProviderSlug *string     `json:"providerSlug"`
	ProviderName *string     `json:"providerName"`
	text         string      // q's text: title, detail, network name, country and target, lower case
}

// hasWords reports whether every word occurs in the row's text.
func (r *EventRow) hasWords(words []string) bool {
	for _, w := range words {
		if !strings.Contains(r.text, w) {
			return false
		}
	}
	return true
}

// EventsAnswer is the /v1/events body. Cursor is the largest event_id the
// answer covers: its largest row id, else the request's after; or, when a q
// read past after stopped at store.TextScan rows, the last row read; never
// above the cursor bound (cursorMarks) unless after is, or before any bound
// is known, the smallest row id minus one (capCursor). More is true, with
// after, when more rows match past Cursor; else when older rows match below
// the page (answer). UpdatedAt is the newest event's at.
type EventsAnswer struct {
	Events    []EventRow `json:"events"`
	Cursor    *int64     `json:"cursor"`
	UpdatedAt *string    `json:"updatedAt"`
	More      bool       `json:"more"`
}

// eventText renders rows with one build's names: products for TapeFromEvents
// and slugs, each network's site slug by its framework provider.
type eventText struct {
	products []TapeProduct
	slugs    map[string]string
}

func (t eventText) render(rows []store.Event) []EventRow {
	js := make([]store.EventJSON, len(rows))
	for i := range rows {
		js[i] = rows[i].JSON()
	}
	tape := TapeFromEvents(js, t.products, "", t.slugs)
	out := make([]EventRow, len(rows))
	for i := range js {
		r := EventRow{EventJSON: js[i], Targets: targetsOf(&js[i]), Title: tape[i].Title, Detail: tape[i].Detail,
			Values: tape[i].Values}
		name := ""
		if slug, ok := t.slugs[js[i].Provider]; ok {
			name = tape[i].ProviderName
			r.ProviderSlug, r.ProviderName = &slug, &name
		}
		parts := []string{r.Title, r.Detail, name}
		for _, p := range []*string{js[i].Country, js[i].Target} {
			if p != nil {
				parts = append(parts, *p)
			}
		}
		r.text = strings.ToLower(strings.Join(parts, "\n"))
		out[i] = r
	}
	return out
}

func newEventWindow(in *store.Inputs, text eventText) EventWindow {
	return EventWindow{Raw: in.Events, JSON: text.render(in.Events), Complete: in.EventsComplete,
		Notable: in.Notable, NotableJSON: text.render(in.Notable), NotableComplete: in.NotableComplete,
		Late: in.Late, LateJSON: text.render(in.Late), LateComplete: in.LateComplete, Stats: in.EventStats, text: text,
		marks: cursorMarks{}.next(in.EventsRead)}
}

// Provider is the framework provider of a network's site slug (network=).
func (w *EventWindow) Provider(slug string) (string, bool) {
	for provider, s := range w.text.slugs {
		if s == slug {
			return provider, true
		}
	}
	return "", false
}

// WithEvents is res with the events of in (store.LoadEvents: Events,
// Notable, their completeness, the event counts, Fingerprint.MaxEventID and
// the read's snapshot, added to res's cursor marks) in place of its own,
// and the registered answers with Events built again from them,
// for an events-only change. It is a shallow copy that shares every other
// answer: the dataset's tape and the network answers' tapes were built from
// res's events and keep them until the next full build.
func WithEvents(res *Result, in *store.Inputs) *Result {
	out := *res
	out.Events = newEventWindow(in, res.Events.text)
	out.Events.marks = res.Events.marks.next(in.EventsRead)
	if res.Events.live != nil {
		out.Events.buildLive(res.Events.live)
	}
	out.Fingerprint.MaxEventID = in.Fingerprint.MaxEventID
	return &out
}

// measuredTape is the measured rows of the dataset's tape, one per incident
// (mergeRefs): MeasuredTapeRows of the rows in memory, chosen by
// store.Prioritize, newest first, then the heads (store.Inputs.TapeHeads) not
// among them, so the tape holds each tab's newest store.TapeHeadRows events
// (HOME-TAPE-1).
func (w *EventWindow) measuredTape(heads []store.Event) []store.EventJSON {
	held := mergeRefs(append(refsOf(w.Raw, w.JSON), refsOf(w.Notable, w.NotableJSON)...))
	pick := store.Prioritize(len(held), MeasuredTapeRows, func(i int) string { return held[i].e.Kind })
	out, seen := make([]store.EventJSON, 0, len(pick)+len(heads)), map[any]bool{}
	for _, i := range pick {
		out, seen[rowKey(held[i].e)] = append(out, held[i].j.EventJSON), true
	}
	for _, r := range mergeRefs(refsOf(heads, w.text.render(heads))) {
		if !seen[rowKey(r.e)] {
			out, seen[rowKey(r.e)] = append(out, r.j.EventJSON), true
		}
	}
	return out
}

// rowKey is what tells a row apart once merged: its group, or its id.
func rowKey(e *store.Event) any {
	if e.Merges() {
		return e.Group()
	}
	return e.ID
}

// eventRef is one candidate row of an answer.
type eventRef struct {
	e *store.Event
	j *EventRow
}

// Answer answers a /v1/events filter from memory when the rows held provably
// contain its whole answer; ok=false means the database must be asked.
func (w *EventWindow) Answer(f store.EventFilter) (*EventsAnswer, bool) {
	if f.After != nil {
		// Every row past after is in the window or among the late rows once
		// after reaches below the window's smallest id.
		held := w.Complete || (w.LateComplete && len(w.Raw) > 0 && *f.After >= store.MinEventID(w.Raw)-1)
		if !held {
			return nil, false
		}
		return w.answer(f, append(refsOf(w.Raw, w.JSON), refsOf(w.Late, w.LateJSON)...), false), true
	}
	refs := append(refsOf(w.Raw, w.JSON), refsOf(w.Notable, w.NotableJSON)...)
	if f.Before != nil && !slices.ContainsFunc(refs, func(r eventRef) bool { return r.e.ID == *f.Before }) {
		// Not held: the database places it, or answers ErrUnknownCursor.
		return nil, false
	}
	inWindow, notable := len(matching(f, refs[:len(w.Raw)])), 0
	for _, r := range matching(f, refs) {
		if !store.Routine(r.e.Kind) {
			notable++
		}
	}
	if !w.covers(f, inWindow, notable) {
		return nil, false
	}
	return w.answer(f, refs, false), true
}

func refsOf(rows []store.Event, js []EventRow) []eventRef {
	out := make([]eventRef, len(rows))
	for i := range rows {
		out[i] = eventRef{&rows[i], &js[i]}
	}
	return out
}

// covers reports whether the rows held contain the whole answer of f (without
// after), given its matches (merged rows) in the window and its non-routine
// matches held:
// f.Limit matches and one more (which tells more), or since reaching past the
// window; a prioritized filter also its newest store.NotableQuota non-routine
// matches. With before, the matches are those below its anchor, which the
// window holds.
func (w *EventWindow) covers(f store.EventFilter, inWindow, notable int) bool {
	switch {
	case w.Complete, sinceCovers(f, w.Raw):
		return true
	case !f.Prioritized():
		return inWindow > f.Limit
	}
	return inWindow > f.Limit && (w.NotableComplete || notable >= store.NotableQuota(f.Limit))
}

// FromRows answers f from the rows of the database fallback
// (store.QueryEvents), rendered with the build's names.
func (w *EventWindow) FromRows(f store.EventFilter, rows []store.Event) *EventsAnswer {
	refs := refsOf(rows, w.text.render(rows))
	return w.answer(f, refs, len(f.Words) > 0 && len(rows) >= store.TextScan)
}

// answer picks f's rows (matching) among refs (newest first, or with after in
// any order): with after the oldest by id up to the cursor bound (cursorMarks), for a
// prioritized filter the store.Prioritize choice, else the newest. No cursor
// passes the bound. bounded means refs stop at a q read's bound. More is, with
// after, whether rows follow the cursor; else whether matching rows are left
// out (older ones, but for a prioritized pick's), or a q read stopped there.
func (w *EventWindow) answer(f store.EventFilter, refs []eventRef, bounded bool) *EventsAnswer {
	matched := matching(f, refs)
	bound, settled := w.marks.bound, matched
	if f.After != nil {
		slices.SortFunc(matched, func(a, b eventRef) int { return cmp.Compare(a.e.ID, b.e.ID) })
		if n := slices.IndexFunc(matched, func(r eventRef) bool { return r.e.ID > bound }); n >= 0 {
			settled = matched[:n]
		}
	}
	out := settled[:min(len(settled), f.Limit)]
	if f.Prioritized() {
		out = []eventRef{}
		for _, k := range store.Prioritize(len(matched), f.Limit, func(k int) string { return matched[k].e.Kind }) {
			out = append(out, matched[k])
		}
	}
	a := &EventsAnswer{Events: make([]EventRow, len(out)), Cursor: f.After, UpdatedAt: w.updatedAt(out)}
	for i, r := range out {
		a.Events[i] = *r.j
		if a.Cursor == nil || r.e.ID > *a.Cursor {
			a.Cursor = &r.e.ID
		}
	}
	if f.After == nil {
		w.capCursor(a)
		a.More = len(matched) > len(out) || bounded
		return a
	}
	for _, r := range matched {
		a.More = a.More || (a.Cursor != nil && r.e.ID > *a.Cursor)
	}
	if bounded && !a.More {
		last := slices.MaxFunc(refs, func(a, b eventRef) int { return cmp.Compare(a.e.ID, b.e.ID) }).e.ID
		last = max(min(last, bound), *a.Cursor)
		a.Cursor, a.More = &last, true
	}
	return a
}

// updatedAt is the newest at of the events held and the rows returned.
func (w *EventWindow) updatedAt(out []eventRef) *string {
	var newest *store.Event
	if len(w.Raw) > 0 {
		newest = &w.Raw[0]
	}
	for _, r := range out {
		if newest == nil || r.e.At.After(newest.At) {
			newest = r.e
		}
	}
	if newest == nil {
		return nil
	}
	at := JSISO(newest.At)
	return &at
}

// sinceCovers reports whether rows (newest first) reach back to f.Since, so
// nothing older than them can match.
func sinceCovers(f store.EventFilter, rows []store.Event) bool {
	return f.Since != nil && len(rows) > 0 && !rows[len(rows)-1].At.After(*f.Since)
}
