package server

import (
	"context"
	"errors"
	"sync/atomic"
	"testing"
	"time"

	"github.com/shifter-io/atlas/api/internal/store"
)

// eventSource is fakeSource with the real fingerprint's events part: the
// newest event id (store.Fingerprint.MaxEventID), so a new event is an
// events-only change. It counts its events reads. (This file uses only what
// main had before API-CACHE-2, so the regression runs there unchanged.)
type eventSource struct {
	*fakeSource
	eventLoads atomic.Int64
}

func newestID(rows []store.Event) *int64 {
	if len(rows) == 0 {
		return nil
	}
	id := rows[0].ID
	return &id
}

func (e *eventSource) Fingerprint(ctx context.Context) (store.Fingerprint, error) {
	fp, err := e.fakeSource.Fingerprint(ctx)
	e.mu.Lock()
	defer e.mu.Unlock()
	fp.MaxEventID = newestID(e.events)
	return fp, err
}

func (e *eventSource) LoadEvents(context.Context) (*store.Inputs, error) {
	e.eventLoads.Add(1)
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.fail != nil {
		return nil, e.fail
	}
	in := &store.Inputs{Events: e.events, EventsComplete: e.complete, Notable: e.notable, NotableComplete: !e.notableCut}
	in.Fingerprint.MaxEventID = newestID(e.events)
	return in, nil
}

func (e *eventSource) add(ev store.Event) {
	e.mu.Lock()
	e.events = append([]store.Event{ev}, e.events...)
	e.mu.Unlock()
}

// API-CACHE-2 regression: a new event (only the newest event id changed) does
// not run a full Load, build, render and compression. Every answer but the
// events keeps its Rendered (same pointers: nothing re-rendered or compressed),
// the dataset file is not rebuilt, and /v1/events shows the new event. A data
// change still rebuilds everything.
func TestANewEventRefreshesOnlyTheEventsAnswers(t *testing.T) {
	t.Parallel()
	src := &eventSource{fakeSource: &fakeSource{rev: 1, events: events(30), complete: true}}
	s := newTestServer(t, src)
	before := s.Current()
	if got := listEvents(t, s, "/v1/events"); len(got) != 30 || got[0].ID != 30 {
		t.Fatalf("first events %d", len(got))
	}
	src.add(event(31, time.Date(2026, 10, 1, 13, 0, 0, 0, time.UTC), "pulse_down", "acme"))
	if refreshed, err := s.Refresh(context.Background(), false); err != nil || !refreshed {
		t.Fatalf("new event not picked up: %v %v", refreshed, err)
	}
	after := s.Current()
	if n := src.loads.Load(); n != 1 {
		t.Fatalf("an events-only change ran a full load (%d loads)", n)
	}
	if after.Dataset != before.Dataset || after.DatasetPath != before.DatasetPath || len(after.Static) != len(before.Static) {
		t.Fatal("the dataset file was rebuilt for an events-only change")
	}
	for path, r := range before.Static {
		if after.Static[path] != r {
			t.Errorf("%s was rendered again for an events-only change", path)
		}
	}
	if got := listEvents(t, s, "/v1/events"); len(got) != 31 || got[0].ID != 31 {
		t.Fatalf("events after the refresh: %d, newest %v", len(got), got[0].ID)
	}
	if got := listEvents(t, s, "/v1/events?event=pulse_down"); len(got) != 1 || got[0].ID != 31 || src.dbQueries.Load() != 0 {
		t.Fatalf("filtered events after the refresh %v (%d database queries)", got, src.dbQueries.Load())
	}
	if refreshed, _ := s.Refresh(context.Background(), false); refreshed {
		t.Fatal("refreshed without a change")
	}
	src.set(2, nil)
	if rebuilt, err := s.Refresh(context.Background(), false); err != nil || !rebuilt || src.loads.Load() != 2 {
		t.Fatalf("a data change did not rebuild: %v %v (%d loads)", rebuilt, err, src.loads.Load())
	}
	if s.Current().Dataset == before.Dataset {
		t.Fatal("a data change kept the old dataset file")
	}
}

// A failed events read keeps the previous version serving and reports the
// error; the next successful refresh clears it.
func TestAFailedEventsRefreshKeepsServing(t *testing.T) {
	t.Parallel()
	src := &eventSource{fakeSource: &fakeSource{rev: 1, events: events(3), complete: true}}
	s := newTestServer(t, src)
	before := s.Current()
	src.add(event(4, time.Date(2026, 10, 1, 13, 0, 0, 0, time.UTC), "pulse_down", "acme"))
	src.set(1, errors.New("connection reset"))
	if refreshed, err := s.Refresh(context.Background(), false); err == nil || refreshed || s.Current() != before {
		t.Fatalf("failed events read swapped the version: %v %v", refreshed, err)
	}
	if h := health(t, s); h.Status != "degraded" || h.LastError == nil {
		t.Fatalf("health after a failed events read %+v", h)
	}
	src.set(1, nil)
	if refreshed, err := s.Refresh(context.Background(), false); err != nil || !refreshed {
		t.Fatalf("events not refreshed after the failure: %v %v", refreshed, err)
	}
	if h := health(t, s); h.Status != "ok" || h.LastError != nil || src.loads.Load() != 1 {
		t.Fatalf("health after the recovery %+v (%d loads)", h, src.loads.Load())
	}
}
