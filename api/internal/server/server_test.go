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
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/andybalholm/brotli"
	"github.com/ice-zeus/proxy-index-website/api/internal/store"
)

func sptr(s string) *string { return &s }

// fakeSource serves synthetic inputs; rev changes both the fingerprint and the data.
type fakeSource struct {
	mu        sync.Mutex
	rev       int64
	fail      error
	loads     atomic.Int64
	events    []store.Event
	complete  bool
	dbQueries atomic.Int64
	slowLoad  time.Duration
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
	return fixture(f.rev, f.events, f.complete), nil
}

func (f *fakeSource) Events(_ context.Context, filter store.EventFilter) ([]store.Event, error) {
	f.dbQueries.Add(1)
	out := []store.Event{}
	for i := range f.events {
		if filter.Match(&f.events[i]) && len(out) < filter.Limit {
			out = append(out, f.events[i])
		}
	}
	return out, nil
}

func doc(v string) json.RawMessage { return json.RawMessage(v) }

func fixture(rev int64, events []store.Event, complete bool) *store.Inputs {
	gen := time.Date(2026, 9, 26, 14, 56, 8, 606e6, time.UTC)
	in := &store.Inputs{Fingerprint: store.Fingerprint{SiteRevision: rev}, ReadAt: gen, Events: events, EventsComplete: complete,
		Datasets: []store.DatasetRow{{Name: store.DemoDatasetName, SchemaVersion: "0.6.0", GeneratedAt: gen, IsDemo: true}},
		Targets:  []store.TargetRow{{ID: "generic_http", Label: sptr("Generic HTTP"), Category: sptr("Baseline"), PassCondition: sptr("GET"), IsDemo: false}},
	}
	for i, slug := range []string{"alpha-net", "beta-proxy"} {
		name := fmt.Sprintf("%s r%d", strings.Title(strings.Split(slug, "-")[0]), rev) //nolint:staticcheck
		in.Providers = append(in.Providers, store.ProviderRow{Slug: slug, Name: name, Kinds: []string{"residential"}, PrimaryKind: sptr("residential"),
			MeasuredProduct: sptr("residential_rotating"), HQ: sptr("Somewhere"), Listed: true, SortOrder: int32(i), IsDemo: true,
			Strengths: []string{"fast"}, Weaknesses: []string{}, BestFor: []string{"scraping"}})
		in.Measurements = append(in.Measurements, store.MeasurementRow{ProviderSlug: slug, Category: "residential", ProductKind: "residential_rotating",
			Pool:        doc(`{"uniqueIps24h": 1000, "uniqueIps30d": 9000, "churnRate": 0.5, "recurringShare": 0.2}`),
			Performance: doc(fmt.Sprintf(`{"successRate": 0.9%d, "latencyP50": %d, "latencyP90": 900, "latencyP99": 2000, "requests24h": 1000}`, i, 400+i*100)),
			Targets:     doc(`[]`), Series: doc(`{"success24h": [{"t": 5, "v": 0.9}], "success30d": [{"t": 1440, "v": 0.9}], "latency30d": [{"t": 1440, "v": 400}]}`),
			IPSamples: doc(`[]`), Incidents: doc(`[]`), Geo: doc(`[{"countryCode": "DE", "country": "Germany", "ips": 10, "share": 1}]`), Asns: doc(`[]`),
			Overlap: doc(`{"exclusiveShare": 1, "sharedShare": 0, "overlaps": [], "likelyUpstream": null}`), IsDemo: true})
	}
	return in
}

func newTestServer(t testing.TB, src Source) *Server {
	t.Helper()
	s := New(src, Options{Interval: 20 * time.Millisecond, SearchLRU: 4, Logger: slog.New(slog.NewTextHandler(io.Discard, nil))})
	if _, err := s.Refresh(context.Background(), true); err != nil {
		t.Fatal(err)
	}
	return s
}

func get(h http.Handler, path string, headers ...string) *httptest.ResponseRecorder {
	req := httptest.NewRequest(http.MethodGet, path, nil)
	for i := 0; i+1 < len(headers); i += 2 {
		req.Header.Set(headers[i], headers[i+1])
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec
}

func TestCacheHeadersEncodingsAndConditionalRequests(t *testing.T) {
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
	if hdr.Get("Cache-Control") != "public, max-age=60, stale-while-revalidate=600" || hdr.Get("Vary") != "Accept-Encoding" ||
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
	h.ServeHTTP(head, httptest.NewRequest(http.MethodHead, "/v1/dataset", nil))
	if head.Code != 200 || head.Body.Len() != 0 || head.Header().Get("Content-Length") != fmt.Sprint(len(identity)) {
		t.Fatalf("HEAD: %d %d", head.Code, head.Body.Len())
	}
	for path, code := range map[string]int{"/v1/overview": 200, "/v1/providers": 200, "/v1/providers/alpha-net": 200, "/v1/providers/nope": 404,
		"/v1/coverage": 200, "/v1/search-index": 200, "/v1/snapshot": 404, "/v1/nothing": 404, "/healthz": 200} {
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
	// The background loop notices a change by itself.
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go s.Run(ctx)
	src.set(3, nil)
	deadline := time.Now().Add(3 * time.Second)
	for !strings.Contains(get(h, "/v1/dataset").Body.String(), "Alpha r3") {
		if time.Now().After(deadline) {
			t.Fatal("refresh loop did not pick up the change")
		}
		time.Sleep(10 * time.Millisecond)
	}
}

func TestFailedRebuildKeepsServingThePreviousVersion(t *testing.T) {
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
	r := get(h, "/healthz")
	json.Unmarshal(r.Body.Bytes(), &health) //nolint:errcheck
	if r.Code != 200 || health.Status != "degraded" || health.LastError == nil || !strings.Contains(*health.LastError, "database exploded") || health.Failures != 1 {
		t.Fatalf("health %d %+v", r.Code, health)
	}
	src.set(2, nil)
	if _, err := s.Refresh(context.Background(), false); err != nil {
		t.Fatal(err)
	}
	json.Unmarshal(get(h, "/healthz").Body.Bytes(), &health) //nolint:errcheck
	if health.Status != "ok" || health.LastError != nil {
		t.Fatalf("health after recovery %+v", health)
	}
}

func TestRefreshIsSingleFlight(t *testing.T) {
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
	s := newTestServer(t, &fakeSource{rev: 1})
	h := s.Handler()
	r := get(h, "/v1/search?q=Alpha")
	var res struct {
		Query    string `json:"query"`
		Sections []struct {
			Group string `json:"group"`
			Items []struct {
				ID string `json:"id"`
			} `json:"items"`
		} `json:"sections"`
	}
	if err := json.Unmarshal(r.Body.Bytes(), &res); err != nil {
		t.Fatal(err)
	}
	if res.Query != "alpha" || len(res.Sections) == 0 || res.Sections[0].Group != "Networks" || res.Sections[0].Items[0].ID != "network:alpha-net" {
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
	if !strings.Contains(empty.Body.String(), `"Jump to"`) {
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
	src := &fakeSource{rev: 1, events: events(30), complete: true}
	s := newTestServer(t, src)
	h := s.Handler()
	var body struct {
		Events []store.EventJSON `json:"events"`
	}
	r := get(h, "/v1/events")
	json.Unmarshal(r.Body.Bytes(), &body) //nolint:errcheck
	if r.Code != 200 || len(body.Events) != 30 || body.Events[0].ID != 30 || r.Header().Get("Cache-Control") != CacheEvents {
		t.Fatalf("default events: %d %d %v", r.Code, len(body.Events), r.Header())
	}
	if body.Events[0].At != "2026-10-01T12:30:00.000000+00:00" || body.Events[0].Product != "other/residential" || body.Events[0].Category != "census" {
		t.Fatalf("event shape %+v", body.Events[0])
	}
	r = get(h, "/v1/events?product=acme&category=census&limit=2")
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
	for _, bad := range []string{"limit=0", "limit=501", "category=nope", "since=yesterday", "foo=1"} {
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
	req := httptest.NewRequest(http.MethodGet, "/v1/dataset", nil)
	req.Header.Set("Accept-Encoding", "br")
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, req)
	}
}

func TestVersionNamesTheDatasetAndAnswersConditionally(t *testing.T) {
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
	if v1.BuiltAt.IsZero() {
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
