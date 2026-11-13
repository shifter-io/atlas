package server

import (
	"context"
	"encoding/json"
	"net/url"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/Zeus-Development/atlas/api/internal/store"
)

// The live tape's parameters are read, and a bad one answers 400.
func TestParseTapeFilters(t *testing.T) {
	t.Parallel()
	provider := func(slug string) (string, bool) { return "acme", slug == "acme-net" }
	q, _ := url.ParseQuery("network=acme-net&country=de&target=generic_http&q=Pulse++DE&proxyKind=mobile&after=42&since=2026-10-01T14:00:00%2B02:00")
	f, err := ParseEventFilter(q, provider)
	if err != nil || f.Provider != "acme" || f.Country != "DE" || f.Target != "generic_http" || !slices.Equal(f.Words, []string{"pulse", "de"}) ||
		f.ProxyKind != "mobile" || *f.After != 42 || !f.Since.Equal(time.Date(2026, 10, 1, 12, 0, 0, 0, time.UTC)) || f.Since.Location() != time.UTC {
		t.Fatalf("filter %+v, %v", f, err)
	}
	for _, bad := range []string{"network=nope", "network=acme", "country=USA", "country=u1", "proxyKind=hosting", "after=-1", "after=x",
		"before=-1", "before=x", "before=1.5", "before=3&after=2", "page=2", "offset=5", "q=" + strings.Repeat("a", 201)} {
		q, _ := url.ParseQuery(bad)
		if _, err := ParseEventFilter(q, provider); err == nil {
			t.Errorf("%s accepted", bad)
		}
	}
	// The default poll's key is the pre-rendered default's (addDefaultEvents).
	if f, err := ParseEventFilter(url.Values{"q": {" "}}, provider); err != nil || eventsKey(f) != eventsKey(store.EventFilter{Limit: 100}) {
		t.Fatalf("an empty q: %+v %v", f, err)
	}
	one, two := int64(1), int64(2)
	keys := map[string]bool{}
	for _, f := range []store.EventFilter{{Limit: 1}, {After: &one, Limit: 1}, {After: &two, Limit: 1}, {Target: "a|b", Limit: 1},
		{Words: []string{"a|b"}, Limit: 1}, {Country: "DE", Limit: 1}, {ProxyKind: "isp", Limit: 1}, {Provider: "acme", Limit: 1},
		{Before: &one, Limit: 1}, {Before: &two, Limit: 1}} {
		keys[eventsKey(f)] = true
	}
	if len(keys) != 10 {
		t.Fatalf("%d keys for 10 filters", len(keys))
	}
	if f, err := ParseEventFilter(url.Values{"before": {"7"}, "category": {"pulse"}}, provider); err != nil || *f.Before != 7 || f.After != nil {
		t.Fatalf("before: %+v %v", f, err)
	}
}

type tapeBody struct {
	Events []struct {
		store.EventJSON
		// Title stays null: the row's English text is not served (#962).
		Title        *string `json:"title"`
		ProviderSlug *string `json:"providerSlug"`
	} `json:"events"`
	Cursor    *int64  `json:"cursor"`
	UpdatedAt *string `json:"updatedAt"`
	More      bool    `json:"more"`
}

func readTape(t *testing.T, s *Server, path string) tapeBody {
	t.Helper()
	r := get(s.Handler(), path)
	var b tapeBody
	if r.Code != 200 || json.Unmarshal(r.Body.Bytes(), &b) != nil {
		t.Fatalf("%s: %d %s", path, r.Code, r.Body.String())
	}
	return b
}

func (b tapeBody) ids() []int64 {
	out := []int64{}
	for _, e := range b.Events {
		out = append(out, e.ID)
	}
	return out
}

// /v1/events answers rows with the tape's text, the cursor fields and the
// new filters, from memory while the window proves the answer and from the
// database fallback otherwise.
func TestEventsServesTapeRowsAndTheCursor(t *testing.T) {
	t.Parallel()
	src := &fakeSource{rev: 1, events: events(30), complete: true}
	s := newTestServer(t, src)
	b := readTape(t, s, "/v1/events?after=10&limit=5")
	if !slices.Equal(b.ids(), []int64{11, 12, 13, 14, 15}) || *b.Cursor != 15 || !b.More || *b.UpdatedAt != "2026-10-01T12:30:00.000000+00:00" {
		t.Fatalf("after 10: %v cursor %v more %v", b.ids(), b.Cursor, b.More)
	}
	if b.Events[0].Title != nil || b.Events[0].ProviderSlug != nil {
		t.Fatalf("row %+v", b.Events[0])
	}
	if b := readTape(t, s, "/v1/events?q=nowhere"); len(b.Events) != 0 || b.Cursor != nil || b.More {
		t.Fatalf("q without a match: %+v", b)
	}
	if b := readTape(t, s, "/v1/events?country=US&target=none&proxyKind=residential"); len(b.Events) != 0 {
		t.Fatalf("target none: %v", b.ids())
	}
	if r := get(s.Handler(), "/v1/events?network=acme"); r.Code != 400 {
		t.Fatalf("a framework id as network: %d", r.Code)
	}
	if src.dbQueries.Load() != 0 {
		t.Fatal("a complete window asked the database")
	}
	src.complete = false
	src.set(2, nil)
	s.Refresh(context.Background(), false) //nolint:errcheck
	// The window holds every row past its smallest id but one, and no late row.
	if b := readTape(t, s, "/v1/events?after=0&limit=5"); !slices.Equal(b.ids(), []int64{1, 2, 3, 4, 5}) || src.dbQueries.Load() != 0 {
		t.Fatalf("after 0 from memory: %v, %d database queries", b.ids(), src.dbQueries.Load())
	}
	// With the late rows cut, a row past after may be outside the window.
	src.lateCut = true
	src.set(3, nil)
	s.Refresh(context.Background(), false) //nolint:errcheck
	if b := readTape(t, s, "/v1/events?after=0&limit=5"); !slices.Equal(b.ids(), []int64{1, 2, 3, 4, 5}) || *b.Cursor != 5 || !b.More {
		t.Fatalf("after 0 from the database: %v cursor %v more %v", b.ids(), b.Cursor, b.More)
	}
	if b := readTape(t, s, "/v1/events?q=pulse&after=0&limit=5"); !slices.Equal(b.ids(), []int64{1, 2, 4, 5, 7}) || *b.Cursor != 7 || !b.More {
		t.Fatalf("q after 0 from the database: %v cursor %v more %v", b.ids(), b.Cursor, b.More)
	}
	if n := src.dbQueries.Load(); n != 2 {
		t.Fatalf("%d database queries, want 2", n)
	}
}
