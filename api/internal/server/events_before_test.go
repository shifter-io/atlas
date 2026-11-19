package server

import (
	"context"
	"slices"
	"strings"
	"testing"
)

// /v1/events pages back with before (EVENTS-BEFORE-1): a full first page
// says more, and its before page continues it, from memory and from the
// database; a before that names no event answers 400 unknown_cursor, and
// before with after, a cursor that is no id, page, offset and a limit above
// 500 answer 400.
func TestEventsBeforeRoute(t *testing.T) {
	t.Parallel()
	src := &fakeSource{rev: 1, events: events(30), complete: true}
	s := newTestServer(t, src)
	if b := readTape(t, s, "/v1/events?limit=5"); !slices.Equal(b.ids(), []int64{30, 29, 28, 27, 26}) || !b.More {
		t.Fatalf("first page: %v more %v", b.ids(), b.More)
	}
	if b := readTape(t, s, "/v1/events?before=26&limit=5"); !slices.Equal(b.ids(), []int64{25, 24, 23, 22, 21}) || !b.More {
		t.Fatalf("before 26: %v more %v", b.ids(), b.More)
	}
	if b := readTape(t, s, "/v1/events?before=23&kind=pulse&limit=5"); !slices.Equal(b.ids(), []int64{22, 20, 19, 17, 16}) || !b.More {
		t.Fatalf("pulses before 23: %v more %v", b.ids(), b.More)
	}
	if b := readTape(t, s, "/v1/events?before=3"); !slices.Equal(b.ids(), []int64{2, 1}) || b.More {
		t.Fatalf("before 3: %v more %v", b.ids(), b.More)
	}
	if n := src.dbQueries.Load(); n != 0 {
		t.Fatalf("a complete window asked the database %d times", n)
	}
	r := get(s.Handler(), "/v1/events?before=999")
	if r.Code != 400 || !strings.Contains(r.Body.String(), "unknown_cursor") {
		t.Fatalf("unknown before: %d %s", r.Code, r.Body.String())
	}
	for _, bad := range []string{"before=1&after=2", "before=x", "before=-3", "page=2", "offset=10", "limit=501", "before=5&limit=501"} {
		if r := get(s.Handler(), "/v1/events?"+bad); r.Code != 400 {
			t.Errorf("%s: %d", bad, r.Code)
		}
	}
	// An incomplete window that cannot prove the page and the row below it
	// asks the database.
	src.complete = false
	src.set(2, nil)
	s.Refresh(context.Background(), false) //nolint:errcheck
	before := src.dbQueries.Load()
	if b := readTape(t, s, "/v1/events?before=5&kind=pulse&limit=5"); !slices.Equal(b.ids(), []int64{4, 2, 1}) || b.More {
		t.Fatalf("pulses before 5 from the database: %v more %v", b.ids(), b.More)
	}
	if n := src.dbQueries.Load() - before; n != 1 {
		t.Fatalf("%d database queries, want 1", n)
	}
}
