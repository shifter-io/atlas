package server

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/Zeus-Development/atlas/api/internal/store"
	"golang.org/x/sync/semaphore"
)

// guardedServer is a test server whose uncached work runs limit at a time.
func guardedServer(t *testing.T, src Source, limit int) *Server {
	t.Helper()
	s := New(src, Options{UncachedLimit: limit, Logger: slog.New(slog.NewTextHandler(io.Discard, nil)), FilesDir: t.TempDir()})
	if _, err := s.Refresh(context.Background(), true); err != nil {
		t.Fatal(err)
	}
	return s
}

// isBusy fails unless rec is SEC-COST-1's 503: the registered error code
// busy, no-store (wantError), Retry-After one second.
func isBusy(t *testing.T, what string, rec *httptest.ResponseRecorder) {
	t.Helper()
	wantError(t, what, rec, http.StatusServiceUnavailable, `{"code":"busy","params":{}}`)
	if got := rec.Header().Get("Retry-After"); got != "1" {
		t.Errorf("%s: Retry-After %q, want 1", what, got)
	}
}

// SEC-COST-1: while every slot of uncached work is taken, a request whose
// answer must be computed (a search, an events answer, a place page) waits
// BusyWait, then answers 503 busy; cached answers (a precomputed one, an LRU
// hit) answer at once, and the page check after a build is never refused. A
// slot freed within BusyWait serves the waiting request, and the admin
// /healthz counts the busy answers.
func TestUncachedWorkIsBoundedAndCachedAnswersNeverWait(t *testing.T) {
	t.Parallel()
	s := guardedServer(t, &fakeSource{rev: 1, events: events(30)}, 2)
	h := s.Handler()
	for _, path := range []string{"/v1/search?q=alpha", "/v1/events"} {
		if r := get(h, path); r.Code != http.StatusOK {
			t.Fatalf("%s: %d", path, r.Code)
		}
	}
	free, _ := s.unservedPages(s.Current())
	if err := s.work.Acquire(context.Background(), 2); err != nil {
		t.Fatal(err)
	}
	// The page check after a build takes no slot: it finds what it finds
	// with every slot free.
	if held, _ := s.unservedPages(s.Current()); fmt.Sprint(held) != fmt.Sprint(free) {
		t.Errorf("page check with every slot taken: %v, with them free: %v", held, free)
	}
	own := httptest.NewRequest(http.MethodGet, "/v1/search?q=gamma", nil)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, own.WithContext(context.WithValue(own.Context(), ownCheck{}, true)))
	if rec.Code != http.StatusOK {
		t.Errorf("the page check's own search with every slot taken: %d", rec.Code)
	}
	start := time.Now()
	for _, path := range []string{"/v1/networks", "/v1/search?q=alpha", "/v1/events"} {
		if r := get(h, path); r.Code != http.StatusOK {
			t.Errorf("cached %s: %d", path, r.Code)
		}
	}
	if took := time.Since(start); took >= BusyWait {
		t.Errorf("cached answers took %v, want under %v", took, BusyWait)
	}
	for _, path := range []string{"/v1/search?q=beta", "/v1/events?limit=7", "/v1/places/city/AA%7Cmade%7Cup?range=30d"} {
		start := time.Now()
		isBusy(t, path, get(h, path))
		if took := time.Since(start); took < BusyWait || took > 10*BusyWait {
			t.Errorf("%s: busy after %v, want after %v", path, took, BusyWait)
		}
	}
	go func() {
		time.Sleep(BusyWait / 4)
		s.work.Release(1)
	}()
	if r := get(h, "/v1/search?q=beta"); r.Code != http.StatusOK {
		t.Errorf("a slot freed within BusyWait: %d", r.Code)
	}
	s.work.Release(1)
	if h, _ := s.health(); h.Busy != 3 {
		t.Errorf("/healthz busy %d, want 3", h.Busy)
	}
}

// busyEvents is a source whose request reads find no connection free
// (DBSource.Requests).
type busyEvents struct{ *fakeSource }

func (busyEvents) Events(context.Context, store.EventFilter) ([]store.Event, error) {
	return nil, ErrBusy
}

// SEC-COST-1: an events answer that must be read from the database answers
// 503 busy when the request reads' connections are taken, and DBSource
// gives ErrBusy then without touching its pool.
func TestEventsReadsBeyondTheirConnectionsAnswerBusy(t *testing.T) {
	t.Parallel()
	s := guardedServer(t, busyEvents{&fakeSource{rev: 1, events: events(30)}}, 2)
	isBusy(t, "events from the database", get(s.Handler(), "/v1/events?product=acme&limit=500"))
	if r := get(s.Handler(), "/v1/events?limit=5"); r.Code != http.StatusOK {
		t.Errorf("events from the window: %d", r.Code)
	}
	gate := semaphore.NewWeighted(1)
	gate.TryAcquire(1)
	start := time.Now()
	// A nil Pool: a read past the gate would panic.
	if _, err := (DBSource{Requests: gate}).Events(context.Background(), store.EventFilter{}); err != ErrBusy {
		t.Errorf("events past a full gate: %v, want ErrBusy", err)
	}
	if took := time.Since(start); took < BusyWait {
		t.Errorf("ErrBusy after %v, want after %v", took, BusyWait)
	}
}
