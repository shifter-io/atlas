package server

import (
	"context"
	"encoding/json"
	"fmt"
	"testing"
	"time"

	"github.com/Zeus-Development/proxy-index/api/internal/build"
	"github.com/Zeus-Development/proxy-index/api/internal/store"
)

var releaseHour = time.Date(2026, 11, 2, 16, 0, 30, 0, time.UTC)

func event(id int64, at time.Time, kind, provider string) store.Event {
	return store.Event{ID: id, At: at, Kind: kind, Provider: sptr(provider), Product: sptr("residential"),
		Severity: "info", Data: json.RawMessage(`{}`)}
}

// releaseBurst is n estimate_hour_released rows, ids from first up, all written
// at releaseHour (one release run), newest first.
func releaseBurst(first int64, n int) []store.Event {
	out := make([]store.Event, n)
	for i := range n {
		id := first + int64(n-1-i)
		out[i] = event(id, releaseHour, "estimate_hour_released", fmt.Sprintf("net%02d", id%18))
	}
	return out
}

func listEvents(t *testing.T, s *Server, path string) []store.EventJSON {
	t.Helper()
	r := get(s.Handler(), path)
	var body struct {
		Events []store.EventJSON `json:"events"`
	}
	if r.Code != 200 {
		t.Fatalf("%s: %d", path, r.Code)
	}
	if err := json.Unmarshal(r.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	return body.Events
}

func countKinds(rows []store.EventJSON) map[string]int {
	out := map[string]int{}
	for _, e := range rows {
		out[e.Kind]++
	}
	return out
}

// TAPE-2: an hour release for 18 networks x 40 countries (720 rows, if it were
// written per country) after a pulse down and a latency rise still leaves both on
// the first page and on the dataset's tape; the hour as the framework writes it
// (one row per network) is 18 rows beside the pulse down.
func TestReleaseBurstNeverHidesIncidents(t *testing.T) {
	down := event(1, releaseHour.Add(-10*time.Minute), "pulse_down", "net00")
	rise := event(2, releaseHour.Add(-20*time.Minute), "latency_rise", "net01")
	rows := append(releaseBurst(3, 18*40), down, rise)
	s := newTestServer(t, &fakeSource{rev: 1, events: rows, complete: true})
	page := listEvents(t, s, "/v1/events")
	kinds := countKinds(page)
	if len(page) != 100 || kinds["pulse_down"] != 1 || kinds["latency_rise"] != 1 || kinds["estimate_hour_released"] != 98 {
		t.Fatalf("first page %v", kinds)
	}
	if page[0].ID != 18*40+2 || page[98].Kind != "pulse_down" || page[99].Kind != "latency_rise" {
		t.Fatalf("not newest first: %d %s %s", page[0].ID, page[98].Kind, page[99].Kind)
	}
	// A poll since an earlier time sees them too, and a named tab still gets exactly the newest rows.
	since := listEvents(t, s, "/v1/events?since=2026-11-02T15:00:00Z&limit=10")
	if k := countKinds(since); len(since) != 10 || k["pulse_down"] != 1 || k["latency_rise"] != 1 {
		t.Fatalf("since page %v", k)
	}
	if census := listEvents(t, s, "/v1/events?category=census&limit=5"); len(census) != 5 || census[0].ID != 18*40+2 {
		t.Fatalf("census tab %d", len(census))
	}
	if tape := tapeIDs(t, s, "/v1/dataset"); !tape["event-1"] || !tape["event-2"] || len(tape) < build.MeasuredTapeRows {
		t.Fatalf("tape lost the incidents: %d rows", len(tape))
	}

	hour := append([]store.Event{event(21, releaseHour.Add(time.Minute), "pulse_down", "net00")}, releaseBurst(3, 18)...)
	s = newTestServer(t, &fakeSource{rev: 2, events: hour, complete: true})
	if k := countKinds(listEvents(t, s, "/v1/events")); k["estimate_hour_released"] != 18 || k["pulse_down"] != 1 || len(k) != 2 {
		t.Fatalf("one hour %v", k)
	}
}

// Rows pushed out of the newest-events window by a burst stay in memory when they
// are notable, so the first page is answered without the database; a filter the
// memory cannot prove goes to the database and gets the same choice.
func TestNotableRowsPastTheWindowAnswerFromMemory(t *testing.T) {
	window := releaseBurst(10, store.EventWindow)
	down := event(1, releaseHour.Add(-time.Hour), "pulse_down", "net05")
	src := &fakeSource{rev: 1, events: window, notable: []store.Event{down}}
	s := newTestServer(t, src)
	page := listEvents(t, s, "/v1/events?limit=4")
	if len(page) != 4 || page[3].ID != 1 || src.dbQueries.Load() != 0 {
		t.Fatalf("first page %v, %d database queries", countKinds(page), src.dbQueries.Load())
	}
	if tape := tapeIDs(t, s, "/v1/dataset"); !tape["event-1"] {
		t.Fatal("tape lost the pulse down past the window")
	}
	// A truncated notable list cannot prove a product it holds no rows for.
	src.notableCut = true
	src.set(2, nil)
	s.Refresh(context.Background(), false) //nolint:errcheck
	page = listEvents(t, s, "/v1/events?product=net05&limit=4")
	if src.dbQueries.Load() != 1 || len(page) != 4 || page[3].ID != 1 {
		t.Fatalf("database fallback %v, %d queries", countKinds(page), src.dbQueries.Load())
	}
}

func tapeIDs(t *testing.T, s *Server, path string) map[string]bool {
	t.Helper()
	var ds struct {
		Tape []struct {
			ID string `json:"id"`
		} `json:"tape"`
	}
	if err := json.Unmarshal(get(s.Handler(), path).Body.Bytes(), &ds); err != nil {
		t.Fatal(err)
	}
	out := map[string]bool{}
	for _, r := range ds.Tape {
		out[r.ID] = true
	}
	return out
}
