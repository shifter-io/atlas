package build

import (
	"cmp"
	"encoding/json"
	"slices"
	"testing"
	"time"

	"github.com/Zeus-Development/proxy-index/api/internal/store"
)

// acmeText is the names of a build with one network, acme-net (framework
// provider acme); provider other has no network.
var acmeText = eventText{products: []TapeProduct{{Provider: "acme", Name: "Acme Net", Kind: "residential"}},
	slugs: map[string]string{"acme": "acme-net"}}

// burst is n events of a detect burst sharing one at, ids 1 to n newest
// first: pulses of acme in US and DE, and of other at every fifth.
func burst(n int) []store.Event {
	out := []store.Event{}
	for id := int64(n); id >= 1; id-- {
		provider, country := "acme", "US"
		if id%2 == 0 {
			country = "DE"
		}
		if id%5 == 0 {
			provider = "other"
		}
		out = append(out, store.Event{ID: id, At: windowBase, Kind: "pulse_finished", Provider: sp(provider), Product: sp("residential"),
			Country: sp(country), Target: sp("generic_http"), Severity: "info", Data: json.RawMessage(`{"requests":100,"success_rate":0.9}`)})
	}
	return out
}

// dbAfter is the database fallback's read of an after filter: the Limit+1
// oldest matching rows past it by id.
func dbAfter(rows []store.Event, f store.EventFilter) []store.Event {
	out := []store.Event{}
	for _, e := range rows {
		if f.Match(&e) {
			out = append(out, e)
		}
	}
	slices.SortFunc(out, func(a, b store.Event) int { return cmp.Compare(a.ID, b.ID) })
	return out[:min(len(out), f.Limit+1)]
}

// walk follows the cursor from after=0 with limit 100, as a poll does, and
// returns every id read and the number of reads.
func walk(t *testing.T, read func(f store.EventFilter) *EventsAnswer) ([]int64, int) {
	t.Helper()
	ids, after, reads := []int64{}, int64(0), 0
	for more := true; more; reads++ {
		if reads > 10 {
			t.Fatal("the cursor does not advance")
		}
		a := read(store.EventFilter{After: &after, Limit: 100})
		ids = append(ids, eventIDs(a)...)
		after, more = *a.Cursor, a.More
	}
	return ids, reads
}

// A burst of 300 events sharing one at, read with limit=100 and after, loses
// none: from memory and from the database fallback alike.
func TestABurstReadWithAfterLosesNone(t *testing.T) {
	rows := burst(300)
	want := make([]int64, 300)
	for i := range want {
		want[i] = int64(i + 1)
	}
	w := newEventWindow(&store.Inputs{Events: rows, EventsComplete: true}, acmeText)
	memory, reads := walk(t, func(f store.EventFilter) *EventsAnswer {
		a, ok := w.Answer(f)
		if !ok {
			t.Fatalf("a complete window asked the database for after=%d", *f.After)
		}
		return a
	})
	if !slices.Equal(memory, want) || reads != 3 {
		t.Fatalf("from memory: %d rows in %d reads", len(memory), reads)
	}
	incomplete := newEventWindow(&store.Inputs{Events: rows[:150], LateComplete: true}, acmeText)
	if _, ok := incomplete.Answer(store.EventFilter{After: sp64(0), Limit: 100}); ok {
		t.Fatal("after older than the window answered from memory")
	}
	if _, ok := incomplete.Answer(store.EventFilter{After: sp64(149), Limit: 100}); ok {
		t.Fatal("after below the window's smallest id but one answered from memory")
	}
	if a, ok := incomplete.Answer(store.EventFilter{After: sp64(150), Limit: 1}); !ok || !slices.Equal(eventIDs(a), []int64{151}) {
		t.Fatalf("after the window's smallest id but one: %v %v", eventIDs(a), ok)
	}
	if a, ok := incomplete.Answer(store.EventFilter{After: sp64(151), Limit: 100}); !ok || len(a.Events) != 100 || a.Events[0].ID != 152 || !a.More {
		t.Fatalf("after inside the window: %v %v", eventIDs(a), ok)
	}
	db, _ := walk(t, func(f store.EventFilter) *EventsAnswer { return incomplete.FromRows(f, dbAfter(rows, f)) })
	if !slices.Equal(db, want) {
		t.Fatalf("from the database: %d rows", len(db))
	}
}

func sp64(v int64) *int64 { return &v }

// Rows carry the tape's text and the network's slug and name (null without a
// network); network= (Provider) gives what product= gives.
func TestEventRowsCarryTheTapeTextAndNetwork(t *testing.T) {
	w := newEventWindow(&store.Inputs{Events: burst(10), EventsComplete: true}, acmeText)
	a, _ := w.Answer(store.EventFilter{Limit: 10})
	acme, other := a.Events[1], a.Events[0] // ids 9 and 10
	if acme.Title != "Pulse in US" || acme.Detail != "Acme Net · 100 requests" ||
		*acme.ProviderSlug != "acme-net" || *acme.ProviderName != "Acme Net" || acme.Kind != "pulse_finished" {
		t.Fatalf("acme row %+v", acme)
	}
	if other.ProviderSlug != nil || other.ProviderName != nil || other.Title != "Pulse in DE" {
		t.Fatalf("a provider without a network: %+v", other)
	}
	byNetwork, _ := w.Answer(store.EventFilter{Provider: "acme", Limit: 10})
	byProduct, _ := w.Answer(store.EventFilter{Product: "acme", Limit: 10})
	if !slices.Equal(eventIDs(byNetwork), []int64{9, 8, 7, 6, 4, 3, 2, 1}) || !slices.Equal(eventIDs(byNetwork), eventIDs(byProduct)) {
		t.Fatalf("network %v, product %v", eventIDs(byNetwork), eventIDs(byProduct))
	}
	if p, ok := w.Provider("acme-net"); !ok || p != "acme" {
		t.Fatalf("provider of acme-net: %q %v", p, ok)
	}
	if _, ok := w.Provider("acme"); ok {
		t.Fatal("a framework id is not a network slug")
	}
}

// q: every word must occur, case-insensitive, in the title, detail, network
// name, country or target; the in-memory proof counts q's matches.
func TestQMatchesEveryWord(t *testing.T) {
	w := newEventWindow(&store.Inputs{Events: burst(10), NotableComplete: true}, acmeText)
	cases := []struct {
		words []string
		want  []int64
	}{
		{[]string{"acme", "de"}, []int64{8, 6, 4, 2}},
		{[]string{"pulse", "generic_http"}, []int64{10, 9, 8, 7, 6, 5, 4, 3, 2, 1}},
		{[]string{"100", "requests", "us"}, []int64{9, 7, 5, 3, 1}},
		{[]string{"acme", "nowhere"}, nil},
	}
	for _, c := range cases {
		a, ok := w.Answer(store.EventFilter{Words: c.words, Limit: 1})
		if len(c.want) == 0 {
			if ok {
				t.Errorf("%v: no match proven without the database", c.words)
			}
			continue
		}
		if !ok || !slices.Equal(eventIDs(a), c.want[:1]) {
			t.Errorf("%v limit 1: %v %v", c.words, eventIDs(a), ok)
		}
		all := w.FromRows(store.EventFilter{Words: c.words, Since: &windowBase, Limit: 50}, burst(10))
		if all.More || len(all.Events) != 0 {
			t.Errorf("%v since the burst: %v", c.words, eventIDs(all))
		}
		if all := w.FromRows(store.EventFilter{Words: c.words, Limit: 50}, burst(10)); !slices.Equal(eventIDs(all), c.want) {
			t.Errorf("%v: %v", c.words, eventIDs(all))
		}
	}
}

// more is true when more rows match past the cursor (with after) or below
// the page (newest first), the cursor stays at after when nothing follows,
// and updatedAt is the newest event's at.
func TestMoreCursorAndUpdatedAt(t *testing.T) {
	w := newEventWindow(&store.Inputs{Events: burst(10), EventsComplete: true}, acmeText)
	a, _ := w.Answer(store.EventFilter{After: sp64(3), Country: "DE", Limit: 2})
	if !slices.Equal(eventIDs(a), []int64{4, 6}) || *a.Cursor != 6 || !a.More || *a.UpdatedAt != "2026-11-03T04:00:00.000000+00:00" {
		t.Fatalf("DE past 3: %v cursor %d more %v at %v", eventIDs(a), *a.Cursor, a.More, *a.UpdatedAt)
	}
	a, _ = w.Answer(store.EventFilter{After: sp64(6), Country: "DE", Limit: 2})
	if !slices.Equal(eventIDs(a), []int64{8, 10}) || a.More {
		t.Fatalf("DE past 6: %v more %v", eventIDs(a), a.More)
	}
	a, _ = w.Answer(store.EventFilter{After: sp64(10), Limit: 2})
	if len(a.Events) != 0 || *a.Cursor != 10 || a.More {
		t.Fatalf("nothing new: %v cursor %v more %v", eventIDs(a), a.Cursor, a.More)
	}
	newest, _ := w.Answer(store.EventFilter{Limit: 2})
	if *newest.Cursor != 10 || !newest.More {
		t.Fatalf("newest first: cursor %d more %v", *newest.Cursor, newest.More)
	}
	empty := newEventWindow(&store.Inputs{EventsComplete: true}, acmeText)
	if a, _ := empty.Answer(store.EventFilter{Limit: 2}); a.Cursor != nil || a.UpdatedAt != nil || a.More || a.Events == nil {
		t.Fatalf("no events: %+v", a)
	}
}

// A q read past after that stops at store.TextScan rows moves the cursor to
// the last row read and says more, so the next read goes on from there.
func TestABoundedQReadMovesTheCursor(t *testing.T) {
	rows := burst(store.TextScan)
	slices.Reverse(rows) // oldest first, as an after read
	w := newEventWindow(&store.Inputs{}, acmeText)
	a := w.FromRows(store.EventFilter{After: sp64(0), Words: []string{"nowhere"}, Limit: 10}, rows)
	if len(a.Events) != 0 || *a.Cursor != int64(store.TextScan) || !a.More {
		t.Fatalf("no match in the read: cursor %d more %v", *a.Cursor, a.More)
	}
	a = w.FromRows(store.EventFilter{After: sp64(0), Words: []string{"acme", "us"}, Limit: 10}, rows[:20])
	if !slices.Equal(eventIDs(a), []int64{1, 3, 7, 9, 11, 13, 17, 19}) || *a.Cursor != 19 || a.More {
		t.Fatalf("an unbounded read: %v cursor %d more %v", eventIDs(a), *a.Cursor, a.More)
	}
}

// A late row (a new id stamped older than the window, as the daily ingest
// writes a test's own times) is held past the window, and an after poll
// returns it; with the late rows cut, after goes to the database.
func TestAnAfterPollReturnsALateRow(t *testing.T) {
	rows := burst(10)
	late := store.Event{ID: 11, At: windowBase.Add(-3 * time.Hour), Kind: "daily_test_finished", Provider: sp("acme"), Product: sp("residential"),
		Country: sp("DE"), Severity: "info", Data: json.RawMessage(`{"status":"ok"}`)}
	w := newEventWindow(&store.Inputs{Events: rows[:5], Late: []store.Event{late}, LateComplete: true}, acmeText)
	a, ok := w.Answer(store.EventFilter{After: sp64(8), Limit: 10})
	if !ok || !slices.Equal(eventIDs(a), []int64{9, 10, 11}) || *a.Cursor != 11 || a.More || a.Events[2].Title != "Daily test done in DE" {
		t.Fatalf("after 8: %v %v", eventIDs(a), ok)
	}
	if a, _ := w.Answer(store.EventFilter{Limit: 10, Since: &windowBase}); len(a.Events) != 0 {
		t.Fatalf("a late row is not in the newest rows: %v", eventIDs(a))
	}
	w.LateComplete = false
	if _, ok := w.Answer(store.EventFilter{After: sp64(8), Limit: 10}); ok {
		t.Fatal("a cut late list answered after from memory")
	}
}
