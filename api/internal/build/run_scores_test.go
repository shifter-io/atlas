package build

import (
	"testing"

	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/snap"
	"github.com/Zeus-Development/atlas/api/internal/store"
	"github.com/Zeus-Development/atlas/api/internal/testscale"
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
	case best.Status != scoreRanked:
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

// A run is ranked on the 30-day window of its kind of every network that
// sells that kind (a network's second kind too, not its primary kind's
// figures), its own network and the networks without the kind left out.
func TestARunIsRankedOnTheKindsOwnWindows(t *testing.T) {
	window := func(rate Num) *snap.Product {
		return &snap.Product{Windows: map[string]snap.Window{"30d": {Metrics: snap.Metrics{SuccessRate: rate, Completed: 1000}}}}
	}
	net := func(slug string, kinds map[string]*snap.Product) *entry {
		return &entry{P: &model.Provider{Slug: slug, PrimaryKind: "residential"}, Snap: kinds}
	}
	b := &builder{ds: &model.Dataset{CommunityRuns: []model.CommunityRun{{ID: "r", ProviderSlug: "own", Product: "mobile_rotating", SuccessRate: 0.85, Requests: 1000}}},
		entries: []*entry{
			net("own", map[string]*snap.Product{"mobile": window(0.1)}),
			net("two", map[string]*snap.Product{"residential": window(0.5), "mobile": window(0.9)}),
			net("mob", map[string]*snap.Product{"mobile": window(0.8)}),
			net("res", map[string]*snap.Product{"residential": window(0.99)}),
		}}
	if err := b.runScores(); err != nil {
		t.Fatal(err)
	}
	if s := b.ds.CommunityRuns[0].SuccessScore; s == nil || *s != (model.MetricScore{Score: 5.5, Rank: 2, RankedOf: 3, Status: scoreRanked}) {
		t.Errorf("run score %+v, want rank 2 of 3 (two's mobile 0.9, the run 0.85, mob 0.8)", s)
	}
	if runKind("isp_static") != "isp" || runKind("unknown") != "" {
		t.Errorf("runKind: %q %q", runKind("isp_static"), runKind("unknown"))
	}
}
