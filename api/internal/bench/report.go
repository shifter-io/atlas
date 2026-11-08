package bench

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

// Report is one bench run: bench.json, and bench.md from Markdown.
type Report struct {
	BaseURL   string    `json:"baseUrl"`
	StartedAt time.Time `json:"startedAt"`
	// Revision is /v1/version's at the start; VersionChanged is true when
	// it changed during the run, so some cold requests were not cold.
	Revision       string          `json:"revision"`
	VersionChanged bool            `json:"versionChanged"`
	Warm           int             `json:"warm"`
	Concurrency    int             `json:"concurrency"`
	Build          Build           `json:"build"`
	Pages          []PageResult    `json:"pages"`
	Requests       []RequestResult `json:"requests"`
	// Failures is one line per request over its budget or not answering
	// 200, and per page over its budget.
	Failures []string `json:"failures"`
}

// Build is the last build's timings, from /healthz.
type Build struct {
	Status     string    `json:"status"`
	Version    uint64    `json:"version"`
	BuiltAt    time.Time `json:"builtAt"`
	AgeSeconds float64   `json:"ageSeconds"`
	BuildMs    int64     `json:"buildMs"`
	LoadMs     int64     `json:"loadMs"`
	AssembleMs int64     `json:"assembleMs"`
	RenderMs   int64     `json:"renderMs"`
}

// RequestResult is one request's measurement and budget.
type RequestResult struct {
	URL           string  `json:"url"`
	Class         string  `json:"class"`
	Status        int     `json:"status"`
	Error         string  `json:"error,omitempty"`
	WarmErrors    int     `json:"warmErrors"`
	IdentityBytes int64   `json:"identityBytes"`
	EncodedBytes  int64   `json:"encodedBytes"`
	Encoding      string  `json:"encoding"`
	ColdMs        float64 `json:"coldMs"`
	P50Ms         float64 `json:"p50Ms"`
	P95Ms         float64 `json:"p95Ms"`
	P99Ms         float64 `json:"p99Ms"`
	MaxMs         float64 `json:"maxMs"`
	Budget        Budget  `json:"budget"`
	Over          string  `json:"over,omitempty"`
}

// PageResult is one page: its requests and the sums over them.
type PageResult struct {
	Name     string   `json:"name"`
	Requests []string `json:"requests"`
	ColdMs   float64  `json:"coldMs"`
	P50Ms    float64  `json:"p50Ms"`
	P95Ms    float64  `json:"p95Ms"`
	P99Ms    float64  `json:"p99Ms"`
	MaxMs    float64  `json:"maxMs"`
	BudgetMs float64  `json:"budgetMs"`
	Over     bool     `json:"over"`
}

func pageResults(pages []Page) []PageResult {
	out := make([]PageResult, len(pages))
	for i, p := range pages {
		out[i] = PageResult{Name: p.Name, Requests: p.Requests}
	}
	return out
}

// Failed reports whether a budget is exceeded or a request failed.
func (r *Report) Failed() bool { return len(r.Failures) > 0 }

// judge checks every request and page against b, sums the pages and sorts
// them slowest first.
func (r *Report) judge(b Budgets) {
	by := map[string]*RequestResult{}
	r.Failures = []string{}
	for i := range r.Requests {
		q := &r.Requests[i]
		by[q.URL] = q
		q.Budget = b.of(q.URL)
		q.Over = over(q)
		if q.Over != "" {
			r.Failures = append(r.Failures, q.URL+": "+q.Over)
		}
	}
	for i := range r.Pages {
		p := &r.Pages[i]
		for _, u := range p.Requests {
			q := by[u]
			p.ColdMs, p.P50Ms, p.P95Ms, p.P99Ms, p.MaxMs = p.ColdMs+q.ColdMs, p.P50Ms+q.P50Ms, p.P95Ms+q.P95Ms, p.P99Ms+q.P99Ms, p.MaxMs+q.MaxMs
		}
		p.BudgetMs = b.PageMs + b.PageRequestMs*float64(len(p.Requests))
		if p.Over = b.PageMs > 0 && p.P99Ms > p.BudgetMs; p.Over {
			r.Failures = append(r.Failures, fmt.Sprintf("page %s: p99 sum %.2f ms over its %.2f ms budget", p.Name, p.P99Ms, p.BudgetMs))
		}
	}
	sort.SliceStable(r.Pages, func(i, j int) bool { return r.Pages[i].P99Ms > r.Pages[j].P99Ms })
}

// over is why q fails, or "".
func over(q *RequestResult) string {
	switch {
	case q.Error != "":
		return q.Error
	case q.Status != http.StatusOK:
		return fmt.Sprintf("answers %d", q.Status)
	case q.WarmErrors > 0:
		return fmt.Sprintf("%d warm requests failed", q.WarmErrors)
	case q.Budget.ColdMs > 0 && q.ColdMs > q.Budget.ColdMs:
		return fmt.Sprintf("cold %.2f ms over its %.2f ms budget", q.ColdMs, q.Budget.ColdMs)
	case q.Budget.P99Ms > 0 && q.P99Ms > q.Budget.P99Ms:
		return fmt.Sprintf("p99 %.2f ms over its %.2f ms budget", q.P99Ms, q.Budget.P99Ms)
	}
	return ""
}

// Write writes bench.json and bench.md to dir (created 0700), each mode 600.
func (r *Report) Write(dir string) error {
	body, err := json.MarshalIndent(r, "", "  ")
	if err != nil {
		return err
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	for name, b := range map[string][]byte{"bench.json": append(body, '\n'), "bench.md": r.Markdown()} {
		path := filepath.Join(dir, name)
		if err := os.WriteFile(path, b, 0o600); err != nil {
			return err
		}
		// WriteFile keeps the mode of a file that already exists.
		if err := os.Chmod(path, 0o600); err != nil {
			return err
		}
	}
	return nil
}

// Markdown is bench.md: the build, the pages slowest first, every request
// over its budget, then every request.
func (r *Report) Markdown() []byte {
	var b bytes.Buffer
	fmt.Fprintf(&b, "# siteapi bench\n\n%s, %s. Version %d (revision `%s`), built %s, %s old. Each request once cold, then %d times warm over %d workers (keep-alive, `Accept-Encoding: br`).\n\n",
		r.BaseURL, r.StartedAt.Format(time.RFC3339), r.Build.Version, short(r.Revision), r.Build.BuiltAt.Format(time.RFC3339),
		(time.Duration(r.Build.AgeSeconds) * time.Second).String(), r.Warm, r.Concurrency)
	if r.VersionChanged {
		b.WriteString("**The version changed during the run**: the cold times after the change are not all cold.\n\n")
	}
	b.WriteString("## Build\n\n| Status | Build | Load | Assemble | Render |\n|---|---|---|---|---|\n")
	fmt.Fprintf(&b, "| %s | %d ms | %d ms | %d ms | %d ms |\n\n", r.Build.Status, r.Build.BuildMs, r.Build.LoadMs, r.Build.AssembleMs, r.Build.RenderMs)
	if r.Failed() {
		fmt.Fprintf(&b, "## Over budget (%d)\n\n", len(r.Failures))
		for _, f := range r.Failures {
			fmt.Fprintf(&b, "- `%s`\n", f)
		}
		b.WriteString("\n")
	} else {
		b.WriteString("Every request and page is within its budget.\n\n")
	}
	b.WriteString("## Pages, slowest first\n\nSums over each page's requests, in ms.\n\n| Page | Requests | Cold | p50 | p95 | p99 | Max | p99 budget |\n|---|---|---|---|---|---|---|---|\n")
	for _, p := range r.Pages {
		fmt.Fprintf(&b, "| %s%s | %d | %.2f | %.2f | %.2f | %.2f | %.2f | %.2f |\n", p.Name, flag(p.Over), len(p.Requests), p.ColdMs, p.P50Ms, p.P95Ms, p.P99Ms, p.MaxMs, p.BudgetMs)
	}
	b.WriteString("\n## Requests\n\nIn ms; bytes identity / encoded.\n\n| Request | Class | Status | Bytes | Cold | p50 | p95 | p99 | Max | Budget cold / p99 |\n|---|---|---|---|---|---|---|---|---|---|\n")
	for _, q := range r.Requests {
		fmt.Fprintf(&b, "| `%s`%s | %s | %d | %d / %d %s | %.2f | %.2f | %.2f | %.2f | %.2f | %.0f / %.0f |\n", q.URL, flag(q.Over != ""), q.Class, q.Status,
			q.IdentityBytes, q.EncodedBytes, q.Encoding, q.ColdMs, q.P50Ms, q.P95Ms, q.P99Ms, q.MaxMs, q.Budget.ColdMs, q.Budget.P99Ms)
	}
	return b.Bytes()
}

func flag(over bool) string {
	if over {
		return " **over**"
	}
	return ""
}

func short(rev string) string {
	if len(rev) > 12 {
		return rev[:12]
	}
	return strings.TrimSpace(rev)
}
