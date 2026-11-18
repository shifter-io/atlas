package server

import (
	"context"
	"encoding/json"
	"net/url"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/shifter-io/atlas/api/internal/build"
	"github.com/shifter-io/atlas/api/internal/store"
	"github.com/shifter-io/atlas/api/internal/testscale"
)

// The live tape's parameters are read, and a bad one answers 400.
func TestParseTapeFilters(t *testing.T) {
	t.Parallel()
	provider := func(slug string) (string, bool) { return "acme", slug == "acme-net" }
	q, _ := url.ParseQuery("network=acme-net&country=de&target=generic_http&q=Pulse++DE&proxyKind=mobile&after=42&since=2026-10-01T14:00:00%2B02:00")
	f, err := ParseEventFilter(q, provider)
	if err != nil || f.Provider != "acme" || f.Country != "DE" || f.Target != "generic_http" || !slices.Equal(f.Words, []string{"pulse", "de"}) ||
		f.ProxyKind != "mobile" || *f.After != 42 || !f.Since.Equal(time.Date(2026, 10, 1, 12, 0, 0, 999000, time.UTC)) || f.Since.Location() != time.UTC {
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
	if f, err := ParseEventFilter(url.Values{"before": {"7"}, "kind": {"pulse"}}, provider); err != nil || *f.Before != 7 || f.After != nil {
		t.Fatalf("before: %+v %v", f, err)
	}
	// kind= takes groups only; event= codes are read as raw kinds (#993).
	q, _ = url.ParseQuery("kind=pulse,,audit&event=hour_released,down")
	if f, err := ParseEventFilter(q, provider); err != nil || !slices.Equal(f.Groups, []string{"pulse", "audit"}) ||
		!slices.Equal(f.Events, []string{"estimate_hour_released", "down"}) {
		t.Fatalf("kind and event: %+v %v", f, err)
	}
}

// codedRow is a /v1/events row's code fields.
type codedRow struct {
	Kind   string         `json:"kind"`
	Event  string         `json:"event"`
	Values map[string]any `json:"values"`
}

func readCoded(t *testing.T, s *Server, path string) []codedRow {
	t.Helper()
	r := get(s.Handler(), path)
	var b struct {
		Events []codedRow `json:"events"`
	}
	if r.Code != 200 || json.Unmarshal(r.Body.Bytes(), &b) != nil {
		t.Fatalf("%s: %d %s", path, r.Code, r.Body.String())
	}
	return b.Events
}

// Every row of a full tape (every kind) carries its group in kind and its
// event code, the tape row's; values.hour is filled where data.hour is; kind=
// takes groups, event= codes, and q matches codes, from memory and from the
// database fallback (#984, #993).
func TestEventsRowsCarryTheirCodeAndFilterByIt(t *testing.T) {
	t.Parallel()
	src := &fakeSource{rev: 1, events: testscale.TapeEvents(3), complete: true}
	s := newTestServer(t, src)
	rows := readCoded(t, s, "/v1/events?limit=500")
	if len(rows) != len(src.events) {
		t.Fatalf("%d rows of %d", len(rows), len(src.events))
	}
	for i, r := range rows {
		raw := map[string]string{"hour_released": "estimate_hour_released"}[r.Event]
		if raw == "" {
			raw = r.Event
		}
		if !slices.ContainsFunc(src.events, func(e store.Event) bool { return e.Kind == raw }) || r.Kind != store.Category(raw) {
			t.Errorf("row %d: kind %q, event %q", i, r.Kind, r.Event)
		}
		if hour := r.Values["hour"]; r.Event == "ip_overlap" && hour != "2026-11-03T03:00:00.000Z" {
			t.Errorf("ip_overlap row: values.hour %v", hour)
		}
	}
	count := func(path string, want int, ok func(codedRow) bool) {
		t.Helper()
		got := readCoded(t, s, path)
		if len(got) != want {
			t.Errorf("%s: %d rows, want %d", path, len(got), want)
		}
		for _, r := range got {
			if ok == nil || !ok(r) {
				t.Errorf("%s: row %+v", path, r)
			}
		}
	}
	isAudit := func(r codedRow) bool { return r.Kind == "audit" }
	count("/v1/events?kind=audit&limit=500", 10, isAudit)
	count("/v1/events?kind=audit,incident&event=ip_overlap,down&limit=500", 9, func(r codedRow) bool { return r.Event == "ip_overlap" || r.Event == "down" })
	count("/v1/events?event=hour_released&limit=500", 6, func(r codedRow) bool { return r.Event == "hour_released" })
	count("/v1/events?event=ip_overlap&kind=audit&limit=500", 7, func(r codedRow) bool { return r.Event == "ip_overlap" })
	count("/v1/events?event=ip_overlap&kind=pulse&limit=500", 0, nil)
	count("/v1/events?q=overlap&limit=500", 7, func(r codedRow) bool { return r.Event == "ip_overlap" })
	count("/v1/events?q=hour_released&limit=500", 6, func(r codedRow) bool { return r.Event == "hour_released" })
	count("/v1/events?q=requests&limit=500", 0, nil) // English tape text is no longer matched
	if src.dbQueries.Load() != 0 {
		t.Fatal("a complete window asked the database")
	}
	src.complete = false
	src.set(2, nil)
	s.Refresh(context.Background(), false) //nolint:errcheck
	count("/v1/events?event=ip_overlap&limit=500", 7, func(r codedRow) bool { return r.Event == "ip_overlap" })
	count("/v1/events?kind=census&limit=500", 10, func(r codedRow) bool { return r.Kind == "census" })
	if n := src.dbQueries.Load(); n != 2 {
		t.Fatalf("%d database queries, want 2", n)
	}
}

// A poll since an event's served at (milliseconds, CONSISTENCY-1) does not
// get that event again; a since with microseconds is read as it is.
func TestSinceCoversItsMillisecond(t *testing.T) {
	t.Parallel()
	at := time.Date(2026, 11, 13, 10, 0, 0, 123456000, time.UTC)
	for since, after := range map[string]bool{"2026-11-13T10:00:00.123Z": false, "2026-11-13T12:00:00.123+02:00": false,
		"2026-11-13T10:00:00.122Z": true, "2026-11-13T10:00:00.123455+00:00": true, "2026-11-13T10:00:00.123456+00:00": false} {
		f, err := ParseEventFilter(url.Values{"since": {since}}, nil)
		if err != nil || at.After(*f.Since) != after {
			t.Errorf("since %s: %v, %v; want the event after it: %v", since, f.Since, err, after)
		}
	}
}

type tapeBody struct {
	Events []struct {
		build.EventRow
		// Title stays null: the row's English text is not served (#962).
		Title *string `json:"title"`
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
	if !slices.Equal(b.ids(), []int64{11, 12, 13, 14, 15}) || *b.Cursor != 15 || !b.More || *b.UpdatedAt != "2026-10-01T12:30:00.000Z" {
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
