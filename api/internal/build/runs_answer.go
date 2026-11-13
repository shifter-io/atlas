package build

import (
	"cmp"
	"fmt"
	"net/http"
	"slices"
	"strconv"
	"strings"

	"github.com/Zeus-Development/atlas/api/internal/model"
)

// /v1/networks/{slug}/runs?kind=&status=&page= (api/answers/runs.md): every
// community run of a network, filtered and paged, with the medians of its
// accepted runs (NET-RUNS-PAGED). The build holds each network's list; a
// page is cut from it on first request. "all" is every network's runs.
func init() {
	RegisterAnswer(PageAnswer{
		Name:    "runs",
		Route:   "GET /v1/networks/{slug}/runs",
		Key:     runsKey,
		Build:   buildRuns,
		Cut:     cutRuns,
		Samples: []string{"/v1/networks/all/runs", "/v1/networks/all/runs?kind=residential&status=accepted&page=1"},
	})
}

var (
	runStatuses = []string{"any", "accepted", "pending", "rejected"}
	// runKinds is the kind= values; without one, the network's primary kind
	// (stored as "primary"; any for "all").
	runKinds = append([]string{"any"}, model.ProxyKinds...)
)

// RunCounts counts the runs of the kind, whatever the status filter.
type RunCounts struct {
	Accepted int `json:"accepted"`
	Total    int `json:"total"`
}

// RunAgreement is the medians of the kind's accepted runs, for the page's
// "Do they agree?" block; Runs is how many accepted runs there are.
type RunAgreement struct {
	Runs        int `json:"runs"`
	SuccessRate Num `json:"successRate"`
	// SuccessScore is SuccessRate on the 1-10 scale, absolute as the market's.
	SuccessScore MarketScore `json:"successScore"`
	LatencyP50   Num         `json:"latencyP50"`
	UniqueIps    Num         `json:"uniqueIps"`
	OverlapShare Num         `json:"overlapShare"`
}

// RunsAnswer is one page of a network's community runs, newest first.
// KindDefault is true when no kind was asked for: Kind is then the network's
// primary kind (any for "all").
type RunsAnswer struct {
	GeneratedAt string               `json:"generatedAt"`
	Network     string               `json:"network"`
	Kind        string               `json:"kind"`
	KindDefault bool                 `json:"kindDefault"`
	Status      string               `json:"status"`
	Page        int                  `json:"page"`
	Pages       int                  `json:"pages"`
	Total       int                  `json:"total"`
	Counts      RunCounts            `json:"counts"`
	Agreement   RunAgreement         `json:"agreement"`
	Rows        []model.CommunityRun `json:"rows"`
}

// runList is what the build holds per network: its runs, newest first, and
// its primary kind.
type runList struct {
	generatedAt, slug, primary string
	rows                       []model.CommunityRun
}

func runsKey(r *http.Request) (string, error) {
	slug, err := PathSlug(r)
	if err != nil {
		return "", err
	}
	q, err := Query(r, "kind", "status", "page")
	if err != nil {
		return "", err
	}
	kind, status := q.Get("kind"), q.Get("status")
	switch {
	case kind != "" && !slices.Contains(runKinds, kind):
		return "", BadRequest("kind must be residential, datacenter, mobile, isp or any")
	case status != "" && !slices.Contains(runStatuses, status):
		return "", BadRequest("status must be accepted, pending, rejected or any")
	}
	page, err := QueryPage(q)
	return fmt.Sprintf("/v1/runs/%s/%s/%s/%d", slug, cmp.Or(kind, "primary"), cmp.Or(status, "any"), page), err
}

func buildRuns(c *Context) (map[string]any, error) {
	primary := map[string]string{}
	for _, e := range c.Entries {
		primary[e.P.Slug] = e.P.PrimaryKind
	}
	out := map[string]any{}
	for slug, rows := range recordScopes(c, func(d *ProviderDetail) []model.CommunityRun { return d.CommunityRuns }, c.Dataset.CommunityRuns) {
		out["/v1/runs/"+slug] = &runList{generatedAt: c.GeneratedAt, slug: slug, primary: cmp.Or(primary[slug], "any"), rows: rows}
	}
	return out, nil
}

// cutRuns is the page at path (runsKey's) of its network's list.
func cutRuns(held map[string]any, path string) (any, bool) {
	parts := strings.Split(path, "/") // "", v1, runs, slug, kind, status, page
	if len(parts) != 7 {
		return nil, false
	}
	list, ok := held["/v1/runs/"+parts[3]].(*runList)
	if !ok {
		return nil, false
	}
	kind, status := parts[4], parts[5]
	n, _ := strconv.Atoi(parts[6])
	if kind == "primary" {
		kind = list.primary
	}
	ofKind := filterRows(list.rows, func(r model.CommunityRun) bool { return productOfKind(r.Product, kind) })
	kept := filterRows(ofKind, func(r model.CommunityRun) bool { return status == "any" || r.Status == status })
	page, pages, ok := cutPage(kept, n)
	if !ok {
		return nil, false
	}
	counts, agreement := runSummary(ofKind)
	return &RunsAnswer{GeneratedAt: list.generatedAt, Network: list.slug, Kind: kind, KindDefault: parts[4] == "primary", Status: status,
		Page: n, Pages: pages, Total: len(kept), Counts: counts, Agreement: agreement, Rows: page}, true
}

// productOfKind reports whether a site product kind (such as
// residential_rotating) is of kind, a category or any.
func productOfKind(product, kind string) bool {
	return kind == "any" || product == kind || strings.HasPrefix(product, kind+"_")
}

// runSummary counts the runs and takes the medians of the accepted ones.
func runSummary(runs []model.CommunityRun) (RunCounts, RunAgreement) {
	var success, latency, ips, overlap []float64
	add := func(to []float64, v Num) []float64 {
		if v.OK() {
			return append(to, v.F())
		}
		return to
	}
	counts := RunCounts{Total: len(runs)}
	for _, r := range runs {
		if r.Status != "accepted" {
			continue
		}
		counts.Accepted++
		success, latency = add(success, r.SuccessRate), add(latency, r.LatencyP50)
		ips, overlap = add(ips, r.UniqueIps), add(overlap, r.OverlapShare)
	}
	rate := Num(median(success))
	return counts, RunAgreement{Runs: counts.Accepted, SuccessRate: rate, SuccessScore: absoluteScore(rate, MarketScoreBasis), LatencyP50: Num(median(latency)),
		UniqueIps: Num(median(ips)), OverlapShare: Num(median(overlap))}
}
