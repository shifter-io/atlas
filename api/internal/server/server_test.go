package server

import (
	"bytes"
	"compress/gzip"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"net/url"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/andybalholm/brotli"
	"github.com/shifter-io/atlas/api/internal/build"
	"github.com/shifter-io/atlas/api/internal/store"
)

func sptr(s string) *string { return &s }

// fakeSource serves synthetic inputs; rev changes both the fingerprint and the data.
type fakeSource struct {
	mu       sync.Mutex
	rev      int64
	fail     error
	loads    atomic.Int64
	events   []store.Event
	complete bool
	// notable is the non-routine rows past the window (store.Inputs.Notable);
	// notableCut marks that list as truncated.
	notable    []store.Event
	notableCut bool
	// lateCut marks the late rows (store.Inputs.Late) as truncated.
	lateCut   bool
	dbQueries atomic.Int64
	slowLoad  time.Duration
	// loaded, when set, receives each revision Load returns.
	loaded chan int64
}

func (f *fakeSource) Fingerprint(context.Context) (store.Fingerprint, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return store.Fingerprint{SiteRevision: f.rev}, nil
}

func (f *fakeSource) set(rev int64, fail error) {
	f.mu.Lock()
	f.rev, f.fail = rev, fail
	f.mu.Unlock()
}

func (f *fakeSource) Load(context.Context) (*store.Inputs, error) {
	f.loads.Add(1)
	if f.slowLoad > 0 {
		time.Sleep(f.slowLoad)
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.fail != nil {
		return nil, f.fail
	}
	in := fixture(f.rev, f.events, f.complete)
	in.Notable, in.NotableComplete = f.notable, !f.notableCut
	in.LateComplete = !f.lateCut
	if f.loaded != nil {
		f.loaded <- f.rev
	}
	return in, nil
}

// LoadEvents is the events part of Load (store.LoadEvents), without counting a load.
func (f *fakeSource) LoadEvents(context.Context) (*store.Inputs, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.fail != nil {
		return nil, f.fail
	}
	return &store.Inputs{Events: f.events, EventsComplete: f.complete, Notable: f.notable, NotableComplete: !f.notableCut, LateComplete: !f.lateCut}, nil
}

func (f *fakeSource) Events(_ context.Context, filter store.EventFilter) ([]store.Event, error) {
	f.dbQueries.Add(1)
	rows := append(append([]store.Event{}, f.events...), f.notable...)
	if filter.Before != nil && !slices.ContainsFunc(rows, func(e store.Event) bool { return e.ID == *filter.Before }) {
		return nil, store.ErrUnknownCursor
	}
	// The rows the answer is chosen from, unpicked, as store.QueryEvents reads
	// them (with before, the anchor too).
	out := []store.Event{}
	for i := range rows {
		if filter.Match(&rows[i]) || (filter.Before != nil && rows[i].ID == *filter.Before) {
			out = append(out, rows[i])
		}
	}
	return out, nil
}

// fixture is two catalog networks without a measurement (unlinked site
// rows), plus a site target.
func fixture(rev int64, events []store.Event, complete bool) *store.Inputs {
	gen := time.Date(2026, 9, 26, 14, 56, 8, 606e6, time.UTC)
	in := &store.Inputs{Fingerprint: store.Fingerprint{SiteRevision: rev}, ReadAt: gen, Events: events, EventsComplete: complete,
		Targets: []store.TargetRow{{ID: "generic_http", Label: sptr("Generic HTTP"), Category: sptr("Baseline"), PassCondition: sptr("GET")}},
	}
	for _, slug := range []string{"alpha-net", "beta-proxy"} {
		name := fmt.Sprintf("%s r%d", strings.Title(strings.Split(slug, "-")[0]), rev) //nolint:staticcheck
		in.Providers = append(in.Providers, store.ProviderRow{Slug: slug, Name: name, Kinds: []string{"residential"}, PrimaryKind: sptr("residential"),
			MeasuredProduct: sptr("residential_rotating"), HQCountry: sptr("LT"), Listed: true,
			Strengths: []string{"fast"}, Weaknesses: []string{}, UseCases: []string{"web_scraping"}})
	}
	return in
}

func newTestServer(t testing.TB, src Source) *Server {
	t.Helper()
	s := New(src, Options{Interval: 20 * time.Millisecond, SearchLRU: 4, Logger: slog.New(slog.NewTextHandler(io.Discard, nil)), FilesDir: t.TempDir()})
	if _, err := s.Refresh(context.Background(), true); err != nil {
		t.Fatal(err)
	}
	return s
}

// get requests path and, like a browser or fetch, follows a redirect (the
// dataset's) with the same headers.
func get(h http.Handler, path string, headers ...string) *httptest.ResponseRecorder {
	rec := getOnce(h, path, headers...)
	if rec.Code == http.StatusFound {
		base, _ := url.Parse(path)
		next, _ := url.Parse(rec.Header().Get("Location"))
		return getOnce(h, base.ResolveReference(next).String(), headers...)
	}
	return rec
}

func getOnce(h http.Handler, path string, headers ...string) *httptest.ResponseRecorder {
	req := httptest.NewRequest(http.MethodGet, path, nil)
	for i := 0; i+1 < len(headers); i += 2 {
		req.Header.Set(headers[i], headers[i+1])
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec
}

func TestCacheHeadersEncodingsAndConditionalRequests(t *testing.T) {
	t.Parallel()
	s := newTestServer(t, &fakeSource{rev: 1})
	h := s.Handler()
	plain := get(h, "/v1/dataset")
	if plain.Code != 200 || plain.Header().Get("Content-Encoding") != "" {
		t.Fatalf("identity: %d %v", plain.Code, plain.Header())
	}
	identity := plain.Body.Bytes()
	var ds map[string]any
	if err := json.Unmarshal(identity, &ds); err != nil || len(ds["providers"].([]any)) != 2 {
		t.Fatalf("dataset body: %v", err)
	}
	hdr := plain.Header()
	if hdr.Get("Cache-Control") != CacheFile || hdr.Get("Vary") != "Accept-Encoding" ||
		hdr.Get("Content-Type") != "application/json; charset=utf-8" || hdr.Get("Content-Length") != fmt.Sprint(len(identity)) {
		t.Fatalf("headers %v", hdr)
	}
	etag := hdr.Get("ETag")
	if len(etag) != 66 || strings.HasPrefix(etag, "W/") {
		t.Fatalf("etag %q is not a strong sha256 tag", etag)
	}

	br := get(h, "/v1/dataset", "Accept-Encoding", "gzip, deflate, br")
	if br.Header().Get("Content-Encoding") != "br" || br.Header().Get("ETag") != etag[:65]+`-br"` {
		t.Fatalf("br: %v", br.Header())
	}
	dec, err := io.ReadAll(brotli.NewReader(br.Body))
	if err != nil || !bytes.Equal(dec, identity) {
		t.Fatalf("brotli body does not decode to the identity body: %v", err)
	}
	gz := get(h, "/v1/dataset", "Accept-Encoding", "gzip")
	if gz.Header().Get("Content-Encoding") != "gzip" {
		t.Fatalf("gzip: %v", gz.Header())
	}
	zr, err := gzip.NewReader(gz.Body)
	if err != nil {
		t.Fatal(err)
	}
	if dec, _ := io.ReadAll(zr); !bytes.Equal(dec, identity) {
		t.Fatal("gzip body does not decode to the identity body")
	}
	if get(h, "/v1/dataset", "Accept-Encoding", "br;q=0, gzip").Header().Get("Content-Encoding") != "gzip" {
		t.Fatal("br;q=0 must not get brotli")
	}

	for _, inm := range []string{etag, "W/" + etag, etag[:65] + `-gz"`, `"nope", ` + etag[:65] + `-br"`, "*"} {
		r := get(h, "/v1/dataset", "If-None-Match", inm, "Accept-Encoding", "br")
		if r.Code != http.StatusNotModified || r.Body.Len() != 0 || r.Header().Get("ETag") == "" || r.Header().Get("Cache-Control") == "" {
			t.Fatalf("If-None-Match %s: %d (%d bytes)", inm, r.Code, r.Body.Len())
		}
	}
	if r := get(h, "/v1/dataset", "If-None-Match", `"stale"`); r.Code != 200 {
		t.Fatalf("stale etag: %d", r.Code)
	}
	head := httptest.NewRecorder()
	h.ServeHTTP(head, httptest.NewRequest(http.MethodHead, s.current.Load().DatasetPath, nil))
	if head.Code != 200 || head.Body.Len() != 0 || head.Header().Get("Content-Length") != fmt.Sprint(len(identity)) {
		t.Fatalf("HEAD: %d %d", head.Code, head.Body.Len())
	}
	for path, code := range map[string]int{"/v1/overview": 404, "/v1/providers": 404, "/v1/providers/alpha-net": 404, "/v1/providers/nope": 404,
		// alpha-net is a catalog network without a measurement, so it has no chart.
		"/v1/networks/alpha-net/chart": 404, "/v1/networks/alpha-net/chart?range=24h": 404, "/v1/networks/alpha-net/chart?range=1y": 400,
		"/v1/networks/alpha-net/chart?kind=mobile": 404, "/v1/networks/alpha-net/chart?x=1": 400, "/v1/networks/nope/chart": 404,
		"/v1/networks/all/chart": 200, "/v1/networks/all/chart?range=7d": 200, "/v1/charts/all/all/7d": 404,
		"/v1/coverage": 200, "/v1/search-index": 404, "/v1/snapshot": 404, "/v1/nothing": 404, "/healthz": 200,
		"/v1/coverage/alpha-net": 200, "/v1/coverage/nope": 404, "/v1/networks": 200, "/v1/networks/alpha-net": 200, "/v1/networks/alpha-net?kind=mobile": 404, "/v1/networks/nope": 404,
		"/v1/networks/Bad_Slug": 400, "/v1/networks?x=1": 400, "/v1/networks/alpha-net/places": 200,
		"/v1/networks/alpha-net/places?dimension=asn&range=7d&q=x&page=2": 200, "/v1/networks/alpha-net/places?dimension=street": 404,
		"/v1/networks/alpha-net/places?page=0": 400, "/v1/networks/alpha-net/places?range=1y": 400, "/v1/countries": 200,
		"/v1/countries/DE": 404, "/v1/countries/DEU": 400, "/v1/countries/DE/cities": 404, "/v1/countries/D1/cities": 400,
		"/v1/rankings": 200, "/v1/rankings?range=24h": 200, "/v1/rankings?range=1y": 400, "/v1/rankings?x=1": 400,
		"/v1/rankings?kind=residential": 200, "/v1/rankings?range=7d&kind=mobile": 200, "/v1/rankings?kind=all": 400, "/v1/rankings?kind=x&range=1y": 400,
		"/v1/networks/all": 200, "/v1/networks/all/places?dimension=city": 200, "/v1/networks/all?kind=residential": 404} {
		if r := get(h, path); r.Code != code {
			t.Errorf("%s: %d, want %d", path, r.Code, code)
		}
	}
	post := httptest.NewRecorder()
	h.ServeHTTP(post, httptest.NewRequest(http.MethodPost, "/v1/dataset", nil))
	if post.Code != http.StatusMethodNotAllowed {
		t.Fatalf("POST: %d", post.Code)
	}
}

func TestRefreshRebuildsOnlyOnChange(t *testing.T) {
	t.Parallel()
	src := &fakeSource{rev: 1}
	s := newTestServer(t, src)
	h := s.Handler()
	first := get(h, "/v1/dataset").Header().Get("ETag")
	if rebuilt, err := s.Refresh(context.Background(), false); err != nil || rebuilt {
		t.Fatalf("unchanged fingerprint rebuilt: %v %v", rebuilt, err)
	}
	if src.loads.Load() != 1 {
		t.Fatalf("loads %d", src.loads.Load())
	}
	src.set(2, nil)
	if rebuilt, err := s.Refresh(context.Background(), false); err != nil || !rebuilt {
		t.Fatalf("changed fingerprint not rebuilt: %v %v", rebuilt, err)
	}
	second := get(h, "/v1/dataset")
	if second.Header().Get("ETag") == first || !strings.Contains(second.Body.String(), "Alpha r2") {
		t.Fatal("rebuild did not change the served body")
	}
	// The background loop notices a change by itself. It loads revision 3
	// inside its Refresh, which holds refreshMu until the new version serves,
	// so the test waits for that load and then for the lock, not for a time
	// (a build under -race beside the other tests takes seconds).
	loaded := make(chan int64, 1)
	src.mu.Lock()
	src.loaded = loaded
	src.mu.Unlock()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go s.Run(ctx)
	src.set(3, nil)
	if rev := <-loaded; rev != 3 {
		t.Fatalf("refresh loop loaded revision %d, want 3", rev)
	}
	s.refreshMu.Lock()
	s.refreshMu.Unlock() //nolint:staticcheck // waits for the loop's Refresh to finish
	if !strings.Contains(get(h, "/v1/dataset").Body.String(), "Alpha r3") {
		t.Fatal("refresh loop did not pick up the change")
	}
}

func TestFailedRebuildKeepsServingThePreviousVersion(t *testing.T) {
	t.Parallel()
	src := &fakeSource{rev: 1}
	s := newTestServer(t, src)
	h := s.Handler()
	before := get(h, "/v1/dataset").Body.String()
	src.set(2, errors.New("database exploded"))
	if _, err := s.Refresh(context.Background(), false); err == nil {
		t.Fatal("expected a build error")
	}
	if get(h, "/v1/dataset").Body.String() != before {
		t.Fatal("served body changed after a failed rebuild")
	}
	var health Health
	r := get(s.AdminHandler(), "/healthz")
	json.Unmarshal(r.Body.Bytes(), &health) //nolint:errcheck
	if r.Code != 200 || health.Status != "degraded" || health.LastError == nil || !strings.Contains(*health.LastError, "database exploded") || health.Failures != 1 {
		t.Fatalf("health %d %+v", r.Code, health)
	}
	src.set(2, nil)
	if _, err := s.Refresh(context.Background(), false); err != nil {
		t.Fatal(err)
	}
	json.Unmarshal(get(s.AdminHandler(), "/healthz").Body.Bytes(), &health) //nolint:errcheck
	if health.Status != "ok" || health.LastError != nil {
		t.Fatalf("health after recovery %+v", health)
	}
}

func TestRefreshIsSingleFlight(t *testing.T) {
	t.Parallel()
	src := &fakeSource{rev: 1}
	s := newTestServer(t, src)
	src.slowLoad = 50 * time.Millisecond
	src.set(2, nil)
	var wg sync.WaitGroup
	var rebuilt atomic.Int64
	for i := 0; i < 10; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if ok, _ := s.Refresh(context.Background(), false); ok {
				rebuilt.Add(1)
			}
		}()
	}
	wg.Wait()
	if rebuilt.Load() != 1 || src.loads.Load() != 2 {
		t.Fatalf("%d rebuilds, %d loads for one change", rebuilt.Load(), src.loads.Load())
	}
}

func TestSearchIsServedFromABoundedLRU(t *testing.T) {
	t.Parallel()
	s := newTestServer(t, &fakeSource{rev: 1})
	h := s.Handler()
	r := get(h, "/v1/search?q=Alpha")
	var res struct {
		Query string `json:"query"`
		Items []struct {
			Type string `json:"type"`
			ID   string `json:"id"`
		} `json:"items"`
	}
	if err := json.Unmarshal(r.Body.Bytes(), &res); err != nil {
		t.Fatal(err)
	}
	if res.Query != "alpha" || len(res.Items) == 0 || res.Items[0].Type != "network" || res.Items[0].ID != "alpha-net" {
		t.Fatalf("search result %+v", res)
	}
	v := s.Current()
	// Same normalised query: same cached body, no new entry.
	again := get(h, "/v1/search?q=%20ALPHA%20%20")
	if again.Header().Get("ETag") != r.Header().Get("ETag") || v.search.Len() != 1 {
		t.Fatalf("normalised query not reused (entries %d)", v.search.Len())
	}
	hits, _ := v.search.Stats()
	if hits != 1 {
		t.Fatalf("hits %d", hits)
	}
	for _, q := range []string{"beta", "germany", "generic", "pricing", "overlap", "networks"} {
		get(h, "/v1/search?q="+q)
	}
	if v.search.Len() != 4 {
		t.Fatalf("LRU holds %d entries, capacity 4", v.search.Len())
	}
	if get(h, "/v1/search?q=x&page=2").Code != 400 {
		t.Fatal("unknown parameter accepted")
	}
	empty := get(h, "/v1/search")
	if !strings.Contains(empty.Body.String(), `"items":[{"type":"network"`) {
		t.Fatalf("empty query: %s", empty.Body.String())
	}
	// A new version starts with a fresh LRU.
	if src, ok := s.src.(*fakeSource); ok {
		src.set(2, nil)
	}
	s.Refresh(context.Background(), false) //nolint:errcheck
	if s.Current().search.Len() != 0 {
		t.Fatal("search LRU carried across versions")
	}
}

func events(n int) []store.Event {
	base := time.Date(2026, 10, 1, 12, 0, 0, 0, time.UTC)
	out := []store.Event{}
	for i := n; i >= 1; i-- {
		kind := "pulse_finished"
		if i%3 == 0 {
			kind = "census_finished"
		}
		provider := "acme"
		if i%2 == 0 {
			provider = "other"
		}
		out = append(out, store.Event{ID: int64(i), At: base.Add(time.Duration(i) * time.Minute), Kind: kind, Provider: sptr(provider), Product: sptr("residential"),
			Country: sptr("US"), Signal: 1, Severity: "info", Data: json.RawMessage(`{"requests":100}`)})
	}
	return out
}

func TestEventsEndpoint(t *testing.T) {
	t.Parallel()
	src := &fakeSource{rev: 1, events: events(30), complete: true}
	s := newTestServer(t, src)
	h := s.Handler()
	var body struct {
		Events []build.EventRow `json:"events"`
	}
	r := get(h, "/v1/events")
	json.Unmarshal(r.Body.Bytes(), &body) //nolint:errcheck
	if r.Code != 200 || len(body.Events) != 30 || body.Events[0].ID != 30 || r.Header().Get("Cache-Control") != CacheEvents {
		t.Fatalf("default events: %d %d %v", r.Code, len(body.Events), r.Header())
	}
	if body.Events[0].At != "2026-10-01T12:30:00.000Z" || body.Events[0].Product != "other/residential" || body.Events[0].Kind != "census" {
		t.Fatalf("event shape %+v", body.Events[0])
	}
	r = get(h, "/v1/events?product=acme&kind=census&limit=2")
	json.Unmarshal(r.Body.Bytes(), &body) //nolint:errcheck
	if len(body.Events) != 2 || body.Events[0].ID != 27 || body.Events[1].ID != 21 {
		t.Fatalf("filtered events %+v", body.Events)
	}
	r = get(h, "/v1/events?since=2026-10-01T12:28:00%2B00:00")
	json.Unmarshal(r.Body.Bytes(), &body) //nolint:errcheck
	if len(body.Events) != 2 {
		t.Fatalf("since: %d events", len(body.Events))
	}
	r = get(h, "/v1/events?since=2026-10-01T12:28:00+00:00") // "+" decoded as space by the query parser
	json.Unmarshal(r.Body.Bytes(), &body)                    //nolint:errcheck
	if len(body.Events) != 2 {
		t.Fatalf("since with a raw '+': %d events", len(body.Events))
	}
	for _, bad := range []string{"limit=0", "limit=501", "kind=nope", "category=census", "since=yesterday", "foo=1"} {
		if get(h, "/v1/events?"+bad).Code != 400 {
			t.Errorf("%s accepted", bad)
		}
	}
	if src.dbQueries.Load() != 0 {
		t.Fatal("complete window still queried the database")
	}
	// With an incomplete window, a filter it cannot prove complete goes to the database once, then the LRU.
	src.complete = false
	src.set(2, nil)
	s.Refresh(context.Background(), false) //nolint:errcheck
	get(h, "/v1/events?product=acme&limit=500")
	get(h, "/v1/events?product=acme&limit=500")
	if src.dbQueries.Load() != 1 {
		t.Fatalf("database fallback queries: %d", src.dbQueries.Load())
	}
	get(h, "/v1/events?limit=5") // answerable from the window
	if src.dbQueries.Load() != 1 {
		t.Fatal("window-answerable query hit the database")
	}
}

func BenchmarkCachedDataset(b *testing.B) {
	s := newTestServer(b, &fakeSource{rev: 1})
	h := s.Handler()
	req := httptest.NewRequest(http.MethodGet, s.current.Load().DatasetPath, nil)
	req.Header.Set("Accept-Encoding", "br")
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, req)
	}
}

func TestVersionNamesTheDatasetAndAnswersConditionally(t *testing.T) {
	t.Parallel()
	src := &fakeSource{rev: 1}
	s := newTestServer(t, src)
	h := s.Handler()
	readVersion := func() (VersionInfo, *httptest.ResponseRecorder) {
		rec := get(h, "/v1/version")
		var v VersionInfo
		if rec.Code != 200 {
			t.Fatalf("version status %d", rec.Code)
		}
		if err := json.Unmarshal(rec.Body.Bytes(), &v); err != nil {
			t.Fatal(err)
		}
		return v, rec
	}
	v1, rec := readVersion()
	if cc := rec.Header().Get("Cache-Control"); cc != CacheVersion {
		t.Fatalf("cache-control %q", cc)
	}
	// The revision is the dataset's own ETag, so a client holding either can compare.
	if etag := get(h, "/v1/dataset").Header().Get("ETag"); `"`+v1.Revision+`"` != etag || len(v1.Revision) != 64 {
		t.Fatalf("revision %q does not name the dataset %s", v1.Revision, etag)
	}
	if time.Time(v1.BuiltAt).IsZero() {
		t.Fatal("no builtAt")
	}
	etag := rec.Header().Get("ETag")
	if again := get(h, "/v1/version", "If-None-Match", etag); again.Code != http.StatusNotModified || again.Body.Len() != 0 {
		t.Fatalf("unchanged version: %d %q", again.Code, again.Body.String())
	}
	// An unchanged fingerprint keeps answering 304 to the same tag.
	if _, err := s.Refresh(context.Background(), false); err != nil {
		t.Fatal(err)
	}
	if again := get(h, "/v1/version", "If-None-Match", etag); again.Code != http.StatusNotModified {
		t.Fatalf("version changed without a rebuild: %d", again.Code)
	}
	src.set(2, nil)
	if _, err := s.Refresh(context.Background(), false); err != nil {
		t.Fatal(err)
	}
	changed := get(h, "/v1/version", "If-None-Match", etag)
	if changed.Code != 200 {
		t.Fatalf("changed data still 304: %d", changed.Code)
	}
	v2, _ := readVersion()
	if v2.Revision == v1.Revision {
		t.Fatal("revision did not change with the data")
	}
	// A rebuild whose dataset bytes are identical keeps the revision.
	if _, err := s.Refresh(context.Background(), true); err != nil {
		t.Fatal(err)
	}
	if v3, _ := readVersion(); v3.Revision != v2.Revision {
		t.Fatal("identical data produced a new revision")
	}
}

func TestPageRoutesRefuseBadInputAndWaitForTheFirstBuild(t *testing.T) {
	t.Parallel()
	cold := New(&fakeSource{rev: 1}, Options{}).Handler()
	for _, path := range []string{"/v1/networks/alpha-net", "/v1/networks/alpha-net/places", "/v1/countries/DE/cities", "/v1/rankings"} {
		if r := get(cold, path); r.Code != http.StatusServiceUnavailable {
			t.Errorf("%s before the first build: %d", path, r.Code)
		}
	}
	h := newTestServer(t, &fakeSource{rev: 1}).Handler()
	for path, code := range map[string]int{"/v1/networks/alpha-net/places?q=" + strings.Repeat("x", 201): 400,
		"/v1/networks/Bad_Slug/places": 400, "/v1/networks/alpha-net/places?sort=ips": 400, "/v1/countries/DE/cities?page=x": 400,
		"/v1/countries/DE/cities?range=2d": 400, "/v1/countries/DE?x=1": 400, "/v1/countries?x=1": 400, "/v1/networks/alpha-net?x=1": 400} {
		if r := get(h, path); r.Code != code {
			t.Errorf("%s: %d, want %d", path, r.Code, code)
		}
	}
	// A page is rendered once per version and served from the LRU after that.
	first, again := get(h, "/v1/networks/alpha-net/places?q=a"), get(h, "/v1/networks/alpha-net/places?q=+A+")
	if first.Code != 200 || first.Header().Get("ETag") != again.Header().Get("ETag") {
		t.Fatalf("cached page: %d %q %q", first.Code, first.Header().Get("ETag"), again.Header().Get("ETag"))
	}
}

func TestRankingsAnswerPerRangeInOrder(t *testing.T) {
	t.Parallel()
	h := newTestServer(t, &fakeSource{rev: 1}).Handler()
	for _, rangeID := range []string{"24h", "7d", "30d"} {
		path := "/v1/rankings?range=" + rangeID
		if rangeID == "30d" {
			path = "/v1/rankings"
		}
		var ans build.RankingsAnswer
		if err := json.Unmarshal(get(h, path).Body.Bytes(), &ans); err != nil || ans.Range != rangeID || len(ans.Networks) == 0 {
			t.Fatalf("%s: %+v (%v)", path, ans, err)
		}
		for i, n := range ans.Networks {
			if n.Position != i+1 || n.Slug == "" {
				t.Fatalf("%s row %d: %+v", path, i, n)
			}
		}
	}
}

func TestTheDatasetIsAHashNamedFileBehindARedirect(t *testing.T) {
	t.Parallel()
	src := &fakeSource{rev: 1}
	s := newTestServer(t, src)
	h := s.Handler()
	v := s.current.Load()
	if _, ok := v.Static["/v1/dataset"]; ok || v.Dataset == nil || v.DatasetPath != "/v1/files/dataset-"+Revision(v)+".json" {
		t.Fatalf("dataset file %q", v.DatasetPath)
	}
	redirect := getOnce(h, "/v1/dataset")
	if redirect.Code != http.StatusFound || redirect.Header().Get("Location") != "files/dataset-"+Revision(v)+".json" ||
		redirect.Header().Get("Cache-Control") != CacheVersion || redirect.Body.Len() > 0 {
		t.Fatalf("redirect %d %v", redirect.Code, redirect.Header())
	}
	// Behind a prefix (the site's /api/site/v1/dataset) the relative Location still names the file.
	if loc, _ := url.Parse("/api/site/v1/dataset"); loc.ResolveReference(&url.URL{Path: redirect.Header().Get("Location")}).Path !=
		"/api/site"+v.DatasetPath {
		t.Fatal("the Location does not resolve under a prefix")
	}
	file := getOnce(h, v.DatasetPath)
	if file.Code != 200 || file.Header().Get("Cache-Control") != CacheFile || !bytes.Equal(file.Body.Bytes(), fileBytes(t, v.Dataset)) {
		t.Fatalf("file %d %v", file.Code, file.Header())
	}
	for _, path := range []string{"/v1/files/dataset-0000.json", "/v1/files/other.json"} {
		if r := getOnce(h, path); r.Code != http.StatusNotFound {
			t.Errorf("%s: %d", path, r.Code)
		}
	}
	// A new build names a new file; the previous one is still served as it
	// was (its grace is TestAReplacedFileAnswersUntilItsGraceEnds).
	src.set(2, nil)
	if _, err := s.Refresh(context.Background(), false); err != nil {
		t.Fatal(err)
	}
	v2 := s.current.Load()
	if old := getOnce(h, v.DatasetPath); old.Code != 200 || !bytes.Equal(old.Body.Bytes(), fileBytes(t, v.Dataset)) ||
		getOnce(h, v2.DatasetPath).Code != 200 || v2.DatasetPath == v.DatasetPath {
		t.Fatalf("previous file after a rebuild: %d", old.Code)
	}
	// Any other dataset name redirects, uncached, to the current file.
	moved := getOnce(h, "/v1/files/dataset-"+strings.Repeat("0", 64)+".json")
	if moved.Code != http.StatusFound || moved.Header().Get("Location") != strings.TrimPrefix(v2.DatasetPath, "/v1/files/") ||
		moved.Header().Get("Cache-Control") != CacheNone {
		t.Fatalf("an older file: %d %v", moved.Code, moved.Header())
	}
	cold := New(&fakeSource{rev: 1}, Options{}).Handler()
	if getOnce(cold, "/v1/dataset").Code != http.StatusServiceUnavailable || getOnce(cold, v.DatasetPath).Code != http.StatusServiceUnavailable {
		t.Fatal("before the first build")
	}
}
