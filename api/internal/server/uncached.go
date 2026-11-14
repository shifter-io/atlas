package server

import (
	"context"
	"errors"
	"net/http"
	"runtime"
	"strconv"
	"strings"
	"time"

	"github.com/Zeus-Development/atlas/api/internal/build"
	"github.com/Zeus-Development/atlas/api/internal/store"
	"golang.org/x/sync/semaphore"
)

// The guard on uncached work (SEC-COST-1). Most answers are bodies the
// Version holds or an LRU kept; the others (a list page, search or place
// page cut on request, a /v1/search, an events answer built on request, a
// registered answer's cut) are computed per request, and every new parameter
// value is a new computation that no cache can answer. At most
// Options.UncachedLimit run at once; a request that gets no slot within
// BusyWait answers 503 busy with Retry-After, so a flood of such requests
// cannot take the processors from the cached answers, which never wait.

// BusyWait is how long a request waits for a slot of uncached work (or a
// database read, DBSource.Requests) before it answers 503 busy.
const BusyWait = 100 * time.Millisecond

// DefaultUncachedLimit is the default Options.UncachedLimit (siteapi serve
// --uncached-limit): UncachedLimitFor the processors the Go runtime uses.
func DefaultUncachedLimit() int { return UncachedLimitFor(runtime.GOMAXPROCS(0)) }

// UncachedLimitFor is the uncached work a server of procs processors runs at
// once: half of them, at least one, so the others serve the cached answers.
// On 4 processors flooded by 64 clients, 8 at once (two per processor) let
// cached answers wait 89 ms at p99 (159 ms during a rebuild); 2 kept them at
// 1 ms (25 ms during a rebuild) and computed as many answers.
func UncachedLimitFor(procs int) int { return max(procs/2, 1) }

// ErrBusy is a database read for a request that got no connection within
// BusyWait (DBSource.Events): the request answers 503 busy.
var ErrBusy = errors.New("every request read connection is busy")

// acquireWithin takes one unit of sem, waiting at most wait (and never past
// ctx's end); false when it got none.
func acquireWithin(ctx context.Context, sem *semaphore.Weighted, wait time.Duration) bool {
	if sem.TryAcquire(1) {
		return true
	}
	ctx, cancel := context.WithTimeout(ctx, wait)
	defer cancel()
	return sem.Acquire(ctx, 1) == nil
}

// ownCheck marks the requests of the page check after a build
// (routecheck.go): they take no slot, so the refresh never waits behind
// visitors, and they ask for one page at a time.
type ownCheck struct{}

// startWork takes a slot of uncached work for r and returns its release;
// when none frees within BusyWait it answers 503 busy and returns false.
func (s *Server) startWork(w http.ResponseWriter, r *http.Request) (release func(), ok bool) {
	if r.Context().Value(ownCheck{}) != nil {
		return func() {}, true
	}
	if acquireWithin(r.Context(), s.work, BusyWait) {
		return func() { s.work.Release(1) }, true
	}
	s.busy(w)
	return nil, false
}

// busy answers 503 busy: no-store, Retry-After one second. Counted in the
// admin /healthz busy.
func (s *Server) busy(w http.ResponseWriter) {
	s.busyAnswers.Add(1)
	w.Header().Set("Retry-After", "1")
	writeError(w, http.StatusServiceUnavailable, build.Busy())
}

// computeIn is the body lru holds under key, else the one compute makes
// within a slot of uncached work, added to lru; another request may have
// added it while this one waited. ok is false when the request is answered:
// 503 busy, or what compute answered when it returns nil.
func (s *Server) computeIn(w http.ResponseWriter, r *http.Request, lru *LRU, key string, compute func() *Rendered) (body *Rendered, ok bool) {
	if body, ok = lru.Get(key); ok {
		return body, true
	}
	release, ok := s.startWork(w, r)
	if !ok {
		return nil, false
	}
	defer release()
	if body, ok = lru.Get(key); ok {
		return body, true
	}
	if body = compute(); body == nil {
		return nil, false
	}
	lru.Add(key, body)
	return body, true
}

// rendered is value rendered with cache (Cache-Control); nil when rendering
// failed, answered 500.
func (s *Server) rendered(w http.ResponseWriter, r *http.Request, value any, cache string) *Rendered {
	body, err := Render(http.StatusOK, value, cache, LevelFast)
	if err != nil {
		s.renderFailed(w, r, err)
		return nil
	}
	return body
}

func (s *Server) serveSearch(w http.ResponseWriter, r *http.Request) {
	v := s.current.Load()
	if v == nil {
		s.unavailable(w)
		return
	}
	if !onlyParams(w, r.URL.Query(), "q") {
		return
	}
	q := r.URL.Query().Get("q")
	if len(q) > maxQuery {
		badRequest(w, build.TooLong("q", maxQuery))
		return
	}
	key := strings.Join(build.Tokens(q), " ")
	body, ok := s.computeIn(w, r, v.search, key, func() *Rendered {
		return s.rendered(w, r, v.Result.Search.Search(key), CacheData)
	})
	if ok {
		s.write(w, r, v, body)
	}
}

func eventsBody(res *build.Result, f store.EventFilter) (*build.EventsAnswer, error) {
	out, ok := res.Events.Answer(f)
	if !ok {
		return nil, errNeedDB
	}
	return out, nil
}

var errNeedDB = errors.New("events window does not cover this filter")

func (s *Server) serveEvents(w http.ResponseWriter, r *http.Request) {
	v := s.current.Load()
	if v == nil {
		s.unavailable(w)
		return
	}
	f, err := ParseEventFilter(r.URL.Query(), v.Result.Events.Provider)
	if err != nil {
		badRequest(w, err)
		return
	}
	body, ok := s.computeIn(w, r, v.events, eventsKey(f), func() *Rendered {
		if resp, ok := s.eventsAnswer(w, r, v, f); ok {
			return s.rendered(w, r, resp, CacheEvents)
		}
		return nil
	})
	if ok {
		s.write(w, r, v, body)
	}
}

// eventsAnswer is f's events answer: from the events window, else read from
// the database (Source.Events); ok is false when the request is answered
// (400 unknown_cursor, 503 busy or events_query_failed).
func (s *Server) eventsAnswer(w http.ResponseWriter, r *http.Request, v *Version, f store.EventFilter) (*build.EventsAnswer, bool) {
	if out, err := eventsBody(v.Result, f); err == nil {
		return out, true
	}
	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	rows, err := s.src.Events(ctx, f)
	cancel()
	switch {
	case err == nil:
		return v.Result.Events.FromRows(f, rows), true
	case errors.Is(err, ErrBusy):
		s.busy(w)
	case errors.Is(err, store.ErrUnknownCursor):
		badRequest(w, build.UnknownCursor("before", strconv.FormatInt(*f.Before, 10)))
	default:
		writeError(w, http.StatusServiceUnavailable, build.Unavailable(build.ReasonEventsQueryFails))
		s.log.Error("events fallback query failed", "error", err)
	}
	return nil, false
}
