package bench

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"net/http"
	"os"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/Zeus-Development/atlas/api/internal/build"
)

// Budget is one request's limits in milliseconds; 0 is not checked.
type Budget struct {
	ColdMs float64 `json:"coldMs"`
	P99Ms  float64 `json:"p99Ms"`
}

// Budgets are the limits of every request and page. A class or request a
// budgets file names replaces its default whole.
type Budgets struct {
	Classes map[string]Budget `json:"classes"`
	// Requests overrides a class for one request URL.
	Requests map[string]Budget `json:"requests"`
	// A page's p99 sum may be PageMs plus PageRequestMs per request.
	PageMs        float64 `json:"pageMs"`
	PageRequestMs float64 `json:"pageRequestMs"`
}

// NetworkMs is the network time the bench adds to every staging budget: it
// goes over a real socket, the CI test over loopback.
const NetworkMs = 20

// CIGuardFactor is how much looser CI's guard is than the staging budgets.
// The CI test runs on a shared server whose timing noise alone can double a
// 2 ms p99, so the guard catches an order-of-magnitude regression, not noise;
// the strict budgets are measured on the staging siteapi (siteapi bench).
const CIGuardFactor = 5

// StagingBudgets are PAGE-LATENCY-1's budgets over loopback: the targets the
// bench measures on the staging siteapi and the final report judges.
func StagingBudgets() Budgets {
	return Budgets{Classes: map[string]Budget{
		ClassAnswer: {ColdMs: 2, P99Ms: 2}, ClassEvents: {ColdMs: 2, P99Ms: 2}, ClassSearch: {ColdMs: 5, P99Ms: 5},
		ClassList: {ColdMs: 50, P99Ms: 2}, ClassListSearch: {ColdMs: 50, P99Ms: 50},
	}, Requests: map[string]Budget{}, PageMs: 10}
}

// CIGuard is what the CI page latency test enforces: every staging limit,
// the page sum's included, CIGuardFactor times over.
func CIGuard() Budgets {
	b := StagingBudgets()
	for k, c := range b.Classes {
		b.Classes[k] = Budget{ColdMs: c.ColdMs * CIGuardFactor, P99Ms: c.P99Ms * CIGuardFactor}
	}
	b.PageMs *= CIGuardFactor
	b.PageRequestMs *= CIGuardFactor
	return b
}

// PageBudgetMs is the most a page of n requests may sum to.
func (b Budgets) PageBudgetMs(n int) float64 { return b.PageMs + b.PageRequestMs*float64(n) }

// DefaultBudgets are the staging budgets plus NetworkMs per request.
func DefaultBudgets() Budgets {
	b := StagingBudgets()
	for k, c := range b.Classes {
		b.Classes[k] = Budget{ColdMs: c.ColdMs + NetworkMs, P99Ms: c.P99Ms + NetworkMs}
	}
	b.PageRequestMs = NetworkMs
	return b
}

// LoadBudgets is the defaults with the file at path laid over them.
func LoadBudgets(path string) (Budgets, error) {
	b := DefaultBudgets()
	if path == "" {
		return b, nil
	}
	body, err := os.ReadFile(path)
	if err != nil {
		return b, err
	}
	if err := json.Unmarshal(body, &b); err != nil {
		return b, fmt.Errorf("budgets %s: %w", path, err)
	}
	return b, nil
}

func (b Budgets) of(request string) Budget {
	if r, ok := b.Requests[request]; ok {
		return r
	}
	return b.Classes[Class(request)]
}

// Options is one bench run.
type Options struct {
	BaseURL     string
	Warm        int
	Concurrency int
	Budgets     Budgets
}

// Run benches the siteapi at o.BaseURL.
func Run(ctx context.Context, o Options) (*Report, error) {
	if o.Warm < 1 || o.Concurrency < 1 {
		return nil, errors.New("warm and concurrency must be at least 1")
	}
	base := strings.TrimRight(o.BaseURL, "/")
	clients := make([]*http.Client, o.Concurrency)
	for i := range clients {
		clients[i] = newClient()
	}
	r := &Report{BaseURL: base, StartedAt: time.Now().UTC(), Warm: o.Warm, Concurrency: o.Concurrency}
	c := clients[0]
	if err := getJSON(ctx, c, base+"/healthz", &r.Build); err != nil {
		return nil, err
	}
	r.Build.AgeSeconds = r.StartedAt.Sub(r.Build.BuiltAt).Seconds()
	var ver struct{ Revision string }
	if err := getJSON(ctx, c, base+"/v1/version", &ver); err != nil {
		return nil, err
	}
	r.Revision = ver.Revision
	sh, err := shape(ctx, c, base)
	if err != nil {
		return nil, err
	}
	r.Pages = pageResults(Expand(Specs, sh, build.Answers()))
	r.Requests = measure(ctx, base, clients, uniqueRequests(r.Pages), o.Warm)
	if err := getJSON(ctx, c, base+"/v1/version", &ver); err == nil && ver.Revision != r.Revision {
		r.VersionChanged = true
	}
	r.judge(o.Budgets)
	return r, nil
}

// newClient is one keep-alive connection that asks for no compression.
func newClient() *http.Client {
	return &http.Client{Timeout: 30 * time.Second, Transport: &http.Transport{DisableCompression: true, MaxIdleConnsPerHost: 1}}
}

// shape reads the networks and country codes of the running server.
func shape(ctx context.Context, c *http.Client, base string) (Shape, error) {
	var nets build.NetworksAnswer
	var countries build.CountriesAnswer
	if err := getJSON(ctx, c, base+"/v1/networks", &nets); err != nil {
		return Shape{}, err
	}
	if err := getJSON(ctx, c, base+"/v1/countries", &countries); err != nil {
		return Shape{}, err
	}
	if len(nets.Networks) == 0 {
		return Shape{}, errors.New("/v1/networks lists no network: nothing to bench")
	}
	sh := Shape{Search: nets.Networks[0].Slug, Query: "as"}
	if len(countries.Countries) > 0 {
		sh.Country = countries.Countries[0].CountryCode
		sh.Query = strings.ToLower(sh.Country)
	}
	for _, n := range nets.Networks {
		kinds := n.Kinds
		if len(kinds) == 0 {
			kinds = []string{n.Kind}
		}
		sh.Networks = append(sh.Networks, Network{Slug: n.Slug, Kinds: kinds})
	}
	return sh, nil
}

func getJSON(ctx context.Context, c *http.Client, u string, v any) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	if err != nil {
		return err
	}
	resp, err := c.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("%s answers %d", u, resp.StatusCode)
	}
	return json.NewDecoder(resp.Body).Decode(v)
}

func uniqueRequests(pages []PageResult) []string {
	var out []string
	seen := map[string]bool{}
	for _, p := range pages {
		for _, r := range p.Requests {
			if !seen[r] {
				seen[r] = true
				out = append(out, r)
			}
		}
	}
	return out
}

// hit is one timed request.
type hit struct {
	status   int
	bytes    int64
	encoding string
	ms       float64
	err      error
}

func do(ctx context.Context, c *http.Client, u, encoding string) hit {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	if err != nil {
		return hit{err: err}
	}
	req.Header.Set("Accept-Encoding", encoding)
	start := time.Now()
	resp, err := c.Do(req)
	if err != nil {
		return hit{err: err}
	}
	n, err := io.Copy(io.Discard, resp.Body)
	resp.Body.Close()
	ms := float64(time.Since(start).Microseconds()) / 1000
	return hit{status: resp.StatusCode, bytes: n, encoding: resp.Header.Get("Content-Encoding"), ms: ms, err: err}
}

// measure times every request once cold, in order on one connection, reads
// its identity size, then times it warm times over the workers.
func measure(ctx context.Context, base string, clients []*http.Client, requests []string, warm int) []RequestResult {
	out := make([]RequestResult, len(requests))
	times := make([][]float64, len(requests))
	for i, u := range requests {
		cold := do(ctx, clients[0], base+u, "br")
		ident := do(ctx, clients[0], base+u, "identity")
		out[i] = RequestResult{URL: u, Class: Class(u), Status: cold.status, ColdMs: cold.ms, EncodedBytes: cold.bytes,
			Encoding: cold.encoding, IdentityBytes: ident.bytes}
		if cold.err != nil {
			out[i].Error = cold.err.Error()
		}
		times[i] = make([]float64, warm)
	}
	type job struct{ i, n int }
	jobs := make(chan job)
	var mu sync.Mutex
	var wg sync.WaitGroup
	for _, c := range clients {
		wg.Add(1)
		go func(c *http.Client) {
			defer wg.Done()
			for j := range jobs {
				h := do(ctx, c, base+requests[j.i], "br")
				times[j.i][j.n] = h.ms
				if h.err != nil || h.status != http.StatusOK {
					mu.Lock()
					out[j.i].WarmErrors++
					mu.Unlock()
				}
			}
		}(c)
	}
	for i := range requests {
		for n := 0; n < warm; n++ {
			jobs <- job{i, n}
		}
	}
	close(jobs)
	wg.Wait()
	for i := range out {
		out[i].P50Ms, out[i].P95Ms, out[i].P99Ms, out[i].MaxMs = quantiles(times[i])
	}
	return out
}

// quantiles is the nearest-rank p50, p95, p99 and max of ms.
func quantiles(ms []float64) (p50, p95, p99, top float64) {
	if len(ms) == 0 {
		return
	}
	s := append([]float64(nil), ms...)
	sort.Float64s(s)
	at := func(q float64) float64 { return s[max(int(math.Ceil(q*float64(len(s))))-1, 0)] }
	return at(0.50), at(0.95), at(0.99), s[len(s)-1]
}
