package build

import (
	"fmt"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/store"
)

func fp(v float64) *float64 { return &v }

// net00's 22 residential and 3 datacenter runs: paged per kind (the primary
// one by default) and status, counted, and the accepted ones' medians.
func TestCommunityRunsArePagedByKindAndStatus(t *testing.T) {
	t.Parallel()
	in := pagedInputs(t)
	statuses := []string{"accepted", "pending", "accepted", "rejected"}
	for i := range 22 {
		ips := int64(1000 + i)
		in.CommunityRuns = append(in.CommunityRuns, store.CommunityRunRow{ID: fmt.Sprintf("c%02d", i), ProviderSlug: "net00", ProductKind: "residential_rotating",
			Status: statuses[i%4], SuccessRate: fp(float64(i) / 100), LatencyP50: fp(float64(100 + i)), UniqueIps: &ips, SubmittedAt: hoursBefore(i)})
	}
	for i := range 3 {
		in.CommunityRuns = append(in.CommunityRuns, store.CommunityRunRow{ID: fmt.Sprintf("d%d", i), ProviderSlug: "net00", ProductKind: "datacenter",
			Status: "accepted", SuccessRate: fp(0.9), OverlapShare: fp(0.25), SubmittedAt: hoursBefore(30 + i)})
	}
	res, err := Build(in)
	if err != nil {
		t.Fatal(err)
	}
	page := func(target string) *RunsAnswer { return cut[RunsAnswer](t, res, "runs", target) }
	first := page("/v1/networks/net00/runs")
	if first == nil || first.Kind != "residential" || !first.KindDefault || first.Status != "any" || first.Total != 22 || first.Pages != 2 || len(first.Rows) != 20 || first.Rows[0].ID != "c00" {
		t.Fatalf("default page %+v", first)
	}
	if a := first.Agreement; first.Counts != (RunCounts{Accepted: 11, Total: 22}) || a.Runs != 11 || a.SuccessRate != 0.1 || a.LatencyP50 != 110 || a.UniqueIps != 1010 || a.OverlapShare.OK() {
		t.Errorf("counts %+v, agreement %+v", first.Counts, a)
	}
	for target, want := range map[string]string{
		"/v1/networks/net00/runs?page=2":                           "residential 22 2 c20",
		"/v1/networks/net00/runs?kind=residential&status=accepted": "residential 11 1 c00",
		"/v1/networks/net00/runs?status=pending":                   "residential 6 1 c01",
		"/v1/networks/net00/runs?status=rejected":                  "residential 5 1 c03",
		"/v1/networks/net00/runs?kind=any&page=2":                  "any 25 2 c20",
		"/v1/networks/net00/runs?kind=datacenter&status=accepted":  "datacenter 3 1 d0",
		"/v1/networks/all/runs?kind=datacenter":                    "datacenter 3 1 d0",
		"/v1/networks/all/runs":                                    "any 25 2 c00",
	} {
		ans := page(target)
		if ans == nil || len(ans.Rows) == 0 || fmt.Sprintf("%s %d %d %s", ans.Kind, ans.Total, ans.Pages, ans.Rows[0].ID) != want {
			t.Errorf("%s: %+v, want %s", target, ans, want)
		}
	}
	if explicit := page("/v1/networks/net00/runs?kind=residential"); explicit.KindDefault || explicit.Total != 22 {
		t.Errorf("?kind=residential: %+v", explicit)
	}
	if dc := page("/v1/networks/net00/runs?kind=datacenter"); dc.Agreement.SuccessRate != 0.9 || dc.Agreement.OverlapShare != 0.25 || dc.Agreement.LatencyP50.OK() {
		t.Errorf("datacenter agreement %+v", dc.Agreement)
	}
	if none := page("/v1/networks/net01/runs?kind=mobile&status=accepted"); none == nil || none.Total != 0 || none.Pages != 1 || none.Rows == nil ||
		none.Agreement.Runs != 0 || none.Agreement.SuccessRate.OK() {
		t.Errorf("no runs: %+v", none)
	}
	if page("/v1/networks/net00/runs?status=accepted&page=2") != nil || page("/v1/networks/gone/runs") != nil {
		t.Error("a second page of 11 runs, or an unknown network's")
	}
	if _, found := cutRuns(res.Pages.Held["runs"], "/v1/runs"); found {
		t.Error("a short path was cut")
	}
}

func TestTheRunsKeyRefusesBadQueries(t *testing.T) {
	t.Parallel()
	for target, want := range map[string]string{"/v1/networks/net00/runs": "/v1/runs/net00/primary/any/1",
		"/v1/networks/net00/runs?kind=isp&status=rejected&page=3": "/v1/runs/net00/isp/rejected/3"} {
		if got, err := storedPath(runsKey, target); got != want || err != nil {
			t.Errorf("%s: %q %v", target, got, err)
		}
	}
	badRequest(t, runsKey, map[string]string{"/v1/networks/net00/runs?kind=primary": "kind must be", "/v1/networks/net00/runs?kind=residential_rotating": "kind must be",
		"/v1/networks/net00/runs?status=done": "status must be", "/v1/networks/net00/runs?page=-1": "page must be",
		"/v1/networks/net00/runs?rating=5": "unknown query parameter", "/v1/networks/x_y/runs": "invalid network"})
}

func TestARunIsOfTheKindItsProductStartsWith(t *testing.T) {
	t.Parallel()
	for _, c := range []struct {
		product, kind string
		want          bool
	}{{"residential_rotating", "residential", true}, {"residential", "residential", true}, {"isp", "any", true},
		{"residentials", "residential", false}, {"datacenter_shared", "residential", false}} {
		if got := productOfKind(c.product, c.kind); got != c.want {
			t.Errorf("%s of %s: %v", c.product, c.kind, got)
		}
	}
}
