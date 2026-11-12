package build

import (
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/store"
	"github.com/Zeus-Development/proxy-index/api/internal/testscale"
)

// A community run is scored as a network row: its rate ranked among the
// measured networks of its kind (its own network left out), its requests
// the 100-request minimum; the network answer and the runs answer serve it,
// and the accepted runs' median has its absolute score.
func TestACommunityRunIsRankedAmongTheNetworksOfItsKind(t *testing.T) {
	t.Parallel()
	in, err := testscale.Inputs(testscale.Shape{Networks: 4, Countries: 1, Exits: 3, Tail: 4})
	if err != nil {
		t.Fatal(err)
	}
	many, few := int64(100000), int64(50)
	in.CommunityRuns = []store.CommunityRunRow{
		{ID: "best", ProviderSlug: "net00", ProductKind: "residential_rotating", Status: "accepted", Requests: &many, SuccessRate: fp(1), SubmittedAt: hoursBefore(1)},
		{ID: "few", ProviderSlug: "net00", ProductKind: "residential_rotating", Status: "accepted", Requests: &few, SuccessRate: fp(0.5), SubmittedAt: hoursBefore(2)},
		{ID: "none", ProviderSlug: "net00", ProductKind: "residential_rotating", Status: "pending", SuccessRate: fp(0.5), SubmittedAt: hoursBefore(3)},
	}
	res, err := Build(in)
	if err != nil {
		t.Fatal(err)
	}
	runs := res.Pages.Network["net00/residential"].CommunityRuns
	if len(runs) != 3 {
		t.Fatalf("%d runs on the network answer", len(runs))
	}
	best := runs[0].SuccessScore
	switch {
	case best == nil:
		t.Fatal("no score on the best run")
	case best.Status == scoreRanked && (best.Rank != 1 || best.Score != 10 || best.RankedOf != 4):
		t.Errorf("best run %+v, want rank 1 of 4, score 10", best)
	case best.Status != scoreRanked && best.Status != scoreAllSimilar:
		t.Errorf("best run status %q", best.Status)
	}
	for _, r := range runs[1:] {
		if s := r.SuccessScore; s == nil || s.Status != notEnoughData || s.Score.OK() {
			t.Errorf("run %s: %+v, want not_enough_data", r.ID, s)
		}
	}
	page := cut[RunsAnswer](t, res, "runs", "/v1/networks/net00/runs")
	if page == nil || len(page.Rows) != 3 || page.Rows[0].SuccessScore == nil {
		t.Fatalf("runs page %+v", page)
	}
	if a := page.Agreement.SuccessScore; a.Score != 7.8 || a.Basis != MarketScoreBasis {
		t.Errorf("agreement score %+v, want 7.8 (1 + 9 × the median 0.75)", a)
	}
}
