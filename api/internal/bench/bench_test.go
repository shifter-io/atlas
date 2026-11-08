package bench

import (
	"context"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/Zeus-Development/proxy-index/api/internal/build"
	"github.com/Zeus-Development/proxy-index/api/internal/server"
	"github.com/Zeus-Development/proxy-index/api/internal/store"
	"github.com/Zeus-Development/proxy-index/api/internal/testscale"
)

// probe is a registered answer under every network, so the bench must put
// each network's slug into its sample.
func init() {
	build.RegisterAnswer(build.PageAnswer{
		Name:  "benchprobe",
		Route: "GET /v1/networks/{slug}/benchprobe",
		Key: func(r *http.Request) (string, error) {
			q, err := build.Query(r, "range")
			if err != nil {
				return "", err
			}
			slug, err := build.PathSlug(r)
			if err != nil {
				return "", err
			}
			rangeID, err := build.QueryRange(q)
			return "/v1/benchprobe/" + slug + "/" + rangeID, err
		},
		Build: func(c *build.Context) (map[string]any, error) {
			out := map[string]any{}
			for _, p := range c.Dataset.Providers {
				for _, r := range Ranges {
					out["/v1/benchprobe/"+p.Slug+"/"+r] = map[string]string{"slug": p.Slug, "range": r}
				}
			}
			return out, nil
		},
		Samples: []string{"/v1/networks/any/benchprobe?range=7d"},
	})
}

func page(t *testing.T, pages []Page, name string) Page {
	t.Helper()
	for _, p := range pages {
		if p.Name == name {
			return p
		}
	}
	t.Fatalf("no page %q", name)
	return Page{}
}

func TestExpandEveryNetworkKindAndRange(t *testing.T) {
	sh := Shape{Networks: []Network{{Slug: "a", Kinds: []string{"residential", "mobile"}}, {Slug: "b", Kinds: []string{"residential"}}}, Query: "de", Search: "a"}
	answers := []build.PageAnswer{
		{Name: "targets", Route: "GET /v1/targets", Samples: []string{"/v1/targets?kind=residential&range=7d"}},
		{Name: "reviews", Route: "GET /v1/networks/{slug}/reviews", Samples: []string{"/v1/networks/zz/reviews?page=1"}},
		{Name: "events-stats", Route: "GET /v1/events/stats", Samples: []string{"/v1/events/stats?range=24h"}},
		{Name: "loose", Route: "GET /v1/loose", Samples: []string{"/v1/loose"}},
	}
	pages := Expand(Specs, sh, answers)
	mobile := page(t, pages, "/proxy-index/networks/a (mobile)").Requests
	want := []string{"/v1/networks/a?kind=mobile", "/v1/networks/a/reviews?page=1", "/v1/targets?kind=mobile&range=30d"}
	for _, r := range Ranges {
		want = append(want, "/v1/networks/a/chart?kind=mobile&range="+r, "/v1/networks/a/coverage?kind=mobile&range="+r+"&q=de", "/v1/rankings?range="+r)
	}
	for _, w := range want {
		if !slices.Contains(mobile, w) {
			t.Errorf("network page lacks %s: %v", w, mobile)
		}
	}
	if b := page(t, pages, "/proxy-index/networks/b (residential)").Requests; !slices.Contains(b, "/v1/networks/b/reviews?page=1") || slices.Contains(b, "/v1/networks/a?kind=mobile") {
		t.Errorf("network b: %v", b)
	}
	if o := page(t, pages, "/proxy-index/overlap").Requests; !slices.Equal(o, []string{"/v1/rankings?range=30d"}) {
		t.Errorf("overlap: %v", o)
	}
	if tg := page(t, pages, "/proxy-index/targets").Requests; len(tg) != 3 || tg[0] != "/v1/targets?kind=residential&range=24h" {
		t.Errorf("targets: %v", tg)
	}
	if lt := page(t, pages, "/proxy-index/live-tape").Requests; !slices.Contains(lt, "/v1/events/stats?range=7d") || !slices.Contains(lt, "/v1/events?limit=200") {
		t.Errorf("live tape: %v", lt)
	}
	if s := page(t, pages, "search palette").Requests; !slices.Equal(s, []string{"/v1/search-index", "/v1/search?q=a"}) {
		t.Errorf("search: %v", s)
	}
	if o := page(t, pages, OtherAnswers).Requests; !slices.Equal(o, []string{"/v1/loose"}) {
		t.Errorf("other: %v", o)
	}
}

func TestClass(t *testing.T) {
	for u, want := range map[string]string{
		"/v1/rankings?range=7d": ClassAnswer, "/v1/events?limit=200": ClassEvents, "/v1/search?q=x": ClassSearch,
		"/v1/networks/a/coverage?range=7d": ClassList, "/v1/networks/a/coverage?range=7d&q=de": ClassListSearch,
		"/v1/countries/DE/cities": ClassList, "/v1/places/city/x?q=y": ClassListSearch, "/v1/networks/a?kind=residential": ClassAnswer,
	} {
		if got := Class(u); got != want {
			t.Errorf("%s: %s, want %s", u, got, want)
		}
	}
}

// source is a three-network v4 build in memory.
type source struct{}

func (source) Fingerprint(context.Context) (store.Fingerprint, error) {
	return store.Fingerprint{SiteRevision: 1}, nil
}

func (source) Load(context.Context) (*store.Inputs, error) {
	return testscale.NetworkInputs(testscale.NetworkShape{Shape: testscale.Shape{Networks: 3, Countries: 2, Exits: 3, Tail: 4, CityAsn: 5}})
}

func (source) LoadEvents(context.Context) (*store.Inputs, error) {
	return &store.Inputs{EventsComplete: true, NotableComplete: true}, nil
}

func (source) Events(context.Context, store.EventFilter) ([]store.Event, error) { return nil, nil }

// serve is the fixture build behind wrap over a real socket.
func serve(t *testing.T, wrap func(http.Handler) http.Handler) string {
	t.Helper()
	s := server.New(source{}, server.Options{FilesDir: t.TempDir()})
	if _, err := s.Refresh(context.Background(), true); err != nil {
		t.Fatal(err)
	}
	ts := httptest.NewServer(wrap(s.Handler()))
	t.Cleanup(ts.Close)
	return ts.URL
}

// loose is every class at a second, the page sums unchecked.
func loose() Budgets {
	b := Budgets{Classes: map[string]Budget{}, Requests: map[string]Budget{}}
	for _, c := range []string{ClassAnswer, ClassEvents, ClassSearch, ClassList, ClassListSearch} {
		b.Classes[c] = Budget{ColdMs: 1000, P99Ms: 1000}
	}
	return b
}

func run(t *testing.T, base string, b Budgets) *Report {
	t.Helper()
	r, err := Run(context.Background(), Options{BaseURL: base, Warm: 5, Concurrency: 2, Budgets: b})
	if err != nil {
		t.Fatal(err)
	}
	return r
}

func TestBenchAFixtureBuild(t *testing.T) {
	r := run(t, serve(t, func(h http.Handler) http.Handler { return h }), loose())
	if r.Failed() || r.Revision == "" || r.Build.Version == 0 {
		t.Fatalf("failures %v, revision %q, build %+v", r.Failures, r.Revision, r.Build)
	}
	probes, networks := 0, map[string]bool{}
	for _, q := range r.Requests {
		if strings.Contains(q.URL, "/benchprobe?") {
			probes++
			networks[strings.Split(q.URL, "/")[3]] = true
		}
		if q.Status != http.StatusOK || q.P50Ms > q.P99Ms || q.P99Ms > q.MaxMs || q.IdentityBytes == 0 {
			t.Errorf("%+v", q)
		}
		if q.URL == "/v1/rankings?range=7d" && (q.Encoding != "br" || q.EncodedBytes >= q.IdentityBytes) {
			t.Errorf("rankings not brotli: %+v", q)
		}
	}
	if len(networks) != 3 || probes != 9 {
		t.Errorf("benchprobe on %d networks, %d requests", len(networks), probes)
	}
	for i := 1; i < len(r.Pages); i++ {
		if r.Pages[i].P99Ms > r.Pages[i-1].P99Ms {
			t.Errorf("pages not slowest first: %s %.3f after %s %.3f", r.Pages[i].Name, r.Pages[i].P99Ms, r.Pages[i-1].Name, r.Pages[i-1].P99Ms)
		}
	}
}

func TestASlowRequestIsOverBudget(t *testing.T) {
	base := serve(t, func(h http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
			if req.URL.Path == "/v1/rankings" && req.URL.Query().Get("range") == "7d" {
				time.Sleep(30 * time.Millisecond)
			}
			h.ServeHTTP(w, req)
		})
	})
	b := loose()
	b.Classes[ClassAnswer] = Budget{ColdMs: 1000, P99Ms: 20}
	b.PageMs, b.PageRequestMs = 1, 1000
	r := run(t, base, b)
	if !r.Failed() || !slices.ContainsFunc(r.Failures, func(f string) bool { return strings.HasPrefix(f, "/v1/rankings?range=7d: p99 ") }) {
		t.Fatalf("failures %v", r.Failures)
	}
	for _, f := range r.Failures {
		if !strings.Contains(f, "range=7d") && !strings.HasPrefix(f, "page ") {
			t.Errorf("a fast request failed: %s", f)
		}
	}
	if md := string(r.Markdown()); !strings.Contains(md, "`/v1/rankings?range=7d` **over**") || !strings.Contains(md, "## Over budget") {
		t.Errorf("bench.md does not flag it:\n%s", md)
	}
}

func TestANotFoundOrServerErrorFails(t *testing.T) {
	base := serve(t, func(h http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
			switch req.URL.Path {
			case "/v1/search-index":
				http.Error(w, "boom", http.StatusInternalServerError)
			case "/v1/events":
				http.NotFound(w, req)
			default:
				h.ServeHTTP(w, req)
			}
		})
	})
	r := run(t, base, loose())
	for _, want := range []string{"/v1/search-index: answers 500", "/v1/events: answers 404", "/v1/events?limit=200: answers 404"} {
		if !slices.Contains(r.Failures, want) {
			t.Errorf("no %q in %v", want, r.Failures)
		}
	}
}

func TestWriteOnlyIntoDir(t *testing.T) {
	parent := t.TempDir()
	dir := filepath.Join(parent, "out")
	r := run(t, serve(t, func(h http.Handler) http.Handler { return h }), loose())
	// An earlier, more open file is replaced at mode 600.
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "bench.md"), nil, 0o644); err != nil {
		t.Fatal(err)
	}
	if err := r.Write(dir); err != nil {
		t.Fatal(err)
	}
	if top, _ := os.ReadDir(parent); len(top) != 1 || top[0].Name() != "out" {
		t.Errorf("parent holds %v", top)
	}
	entries, _ := os.ReadDir(dir)
	if len(entries) != 2 {
		t.Errorf("out holds %v", entries)
	}
	for _, e := range entries {
		info, err := e.Info()
		if err != nil || info.Mode().Perm() != 0o600 || (e.Name() != "bench.json" && e.Name() != "bench.md") {
			t.Errorf("%s: %v %v", e.Name(), info.Mode(), err)
		}
	}
}

func TestBudgets(t *testing.T) {
	d := DefaultBudgets()
	if d.Classes[ClassList] != (Budget{ColdMs: 70, P99Ms: 22}) || d.PageMs != 10 || d.PageRequestMs != NetworkMs {
		t.Errorf("defaults %+v", d)
	}
	path := filepath.Join(t.TempDir(), "budgets.json")
	if err := os.WriteFile(path, []byte(`{"classes":{"answer":{"p99Ms":5}},"requests":{"/v1/version":{"coldMs":1}}}`), 0o600); err != nil {
		t.Fatal(err)
	}
	b, err := LoadBudgets(path)
	if err != nil || b.of("/v1/rankings") != (Budget{P99Ms: 5}) || b.of("/v1/version") != (Budget{ColdMs: 1}) || b.of("/v1/search?q=a") != d.Classes[ClassSearch] {
		t.Errorf("%+v %v", b, err)
	}
	if _, err := LoadBudgets(filepath.Join(t.TempDir(), "none.json")); err == nil {
		t.Error("a missing budgets file loads")
	}
}

func TestQuantiles(t *testing.T) {
	ms := make([]float64, 100)
	for i := range ms {
		ms[i] = float64(100 - i)
	}
	if p50, p95, p99, top := quantiles(ms); p50 != 50 || p95 != 95 || p99 != 99 || top != 100 {
		t.Errorf("%v %v %v %v", p50, p95, p99, top)
	}
}

func TestOver(t *testing.T) {
	b := Budget{ColdMs: 10, P99Ms: 5}
	for want, q := range map[string]RequestResult{
		"":                                       {Status: 200, ColdMs: 10, P99Ms: 5, Budget: b},
		"refused":                                {Error: "refused", Budget: b},
		"answers 503":                            {Status: 503, Budget: b},
		"2 warm requests failed":                 {Status: 200, WarmErrors: 2, Budget: b},
		"cold 11.00 ms over its 10.00 ms budget": {Status: 200, ColdMs: 11, Budget: b},
		"p99 6.00 ms over its 5.00 ms budget":    {Status: 200, P99Ms: 6, Budget: b},
	} {
		if got := over(&q); got != want {
			t.Errorf("%+v: %q, want %q", q, got, want)
		}
	}
	if _, err := Run(context.Background(), Options{BaseURL: "http://127.0.0.1:1", Warm: 0, Concurrency: 1}); err == nil {
		t.Error("warm 0 runs")
	}
	path := filepath.Join(t.TempDir(), "bad.json")
	if err := os.WriteFile(path, []byte("{"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := LoadBudgets(path); err == nil || !strings.Contains(err.Error(), "bad.json") {
		t.Errorf("bad budgets: %v", err)
	}
}
