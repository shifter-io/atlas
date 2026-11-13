package build

import (
	"encoding/json"
	"slices"
	"strings"
	"testing"

	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/snap"
)

// vaRankingRow is e's 30d ranking row as rankingRow fills its 8-target
// figures: windowStats at the provider's list rate over vaCards.
func vaRankingRow(t *testing.T, e *entry) RankingRow {
	t.Helper()
	e.P.Pricing = e.P.ProductMeasurements.Get(valueAllKind).Pricing
	e.P.PrimaryKind = valueAllKind
	ws := windowStats(e.P, e.Snap[valueAllKind], "30d", vaCards)
	return RankingRow{Slug: e.P.Slug, Name: e.P.Name, Source: model.SourceMeasured, Kind: valueAllKind, Requests: 1,
		RowPricing: rowPricing(e.P.Pricing), WindowFigures: ws.WindowFigures}
}

// A ranking row whose 8-target bill misses 2 targets carries the partial
// bill, the count and the missing ids, the figures insights.valueAll has for
// it; its bill and rank stay null with incomplete_targets (VALUE-ALL-PARTIAL-1).
func TestRankingRowsCarryThePartialAllTargetsBill(t *testing.T) {
	t.Parallel()
	gaps := map[string]snap.Metrics{"google_search": vaMetrics(100, 0, 1e8), "cloudflare_challenge": vaMetrics(100, 0, 2e8)}
	c := vaContext(vaEntry("gaps", 2, 0.5, gaps), vaEntry("full", 2, 1, nil))
	ans := &RankingsAnswer{Networks: []RankingRow{vaRankingRow(t, c.Entries[0]), vaRankingRow(t, c.Entries[1])}}
	rankAllTargets(ans.Networks)
	if err := ans.listNA(); err != nil {
		t.Fatal(err)
	}
	va := vaBySlug(homeValueAll(c, vaRows("gaps", "full"), "30d"))
	g, f := ans.Networks[0], ans.Networks[1]
	missing := []string{"google_search", "cloudflare_challenge"}
	if g.CostPer1kAllTargets.OK() || g.ValueRankAllTargets != nil || !vaNear(g.PartialCostPer1kAllTargets, 0.5) ||
		g.TargetsCountedAllTargets != 6 || !slices.Equal(g.MissingTargetsAllTargets, missing) {
		t.Errorf("incomplete row: bill %v rank %v partial %v counted %v missing %v", g.CostPer1kAllTargets,
			g.ValueRankAllTargets, g.PartialCostPer1kAllTargets, g.TargetsCountedAllTargets, g.MissingTargetsAllTargets)
	}
	if v := va["gaps"]; g.PartialCostPer1kAllTargets != v.PartialCostPer1k || int(g.TargetsCountedAllTargets) != v.TargetsCounted ||
		!slices.Equal(g.MissingTargetsAllTargets, v.MissingTargets) {
		t.Errorf("row and insights.valueAll disagree: %v / %v, %v / %v", g.PartialCostPer1kAllTargets, v.PartialCostPer1k,
			g.MissingTargetsAllTargets, v.MissingTargets)
	}
	if g.ReasonOf("costPer1kAllTargets") != NAIncompleteTargets || g.ReasonOf("valueRankAllTargets") != NAIncompleteTargets ||
		g.ReasonOf("partialCostPer1kAllTargets") != "" {
		t.Errorf("incomplete row na %+v", g.NAList)
	}
	if !vaNear(f.CostPer1kAllTargets, 1) || f.CostPer1kAllTargets != va["full"].CostPer1k || f.PartialCostPer1kAllTargets.OK() ||
		f.TargetsCountedAllTargets != 8 || f.MissingTargetsAllTargets == nil || len(f.MissingTargetsAllTargets) != 0 ||
		f.ValueRankAllTargets == nil || *f.ValueRankAllTargets != 1 {
		t.Errorf("complete row: bill %v partial %v counted %v missing %v", f.CostPer1kAllTargets, f.PartialCostPer1kAllTargets,
			f.TargetsCountedAllTargets, f.MissingTargetsAllTargets)
	}
	for _, p := range f.NA {
		if strings.Contains(p, "AllTargets") {
			t.Errorf("complete row lists %s in its na", p)
		}
	}
	raw, err := json.Marshal(f)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(raw), `"partialCostPer1kAllTargets":null,"targetsCountedAllTargets":8,"missingTargetsAllTargets":[]`) {
		t.Errorf("complete row JSON %s", raw)
	}
}

// Without a list rate nothing is missing for a measured reason; without a
// snapshot product the count and ids are null, not_measured.
func TestPartialAllTargetsWithoutAPriceOrASnapshot(t *testing.T) {
	t.Parallel()
	u := vaRankingRow(t, vaEntry("unpriced", nan.F(), 1, nil))
	if u.CostPer1kAllTargets.OK() || u.PartialCostPer1kAllTargets.OK() || u.TargetsCountedAllTargets != 0 ||
		u.MissingTargetsAllTargets == nil || len(u.MissingTargetsAllTargets) != 0 {
		t.Errorf("unpriced row %+v", u.WindowFigures)
	}
	r := RankingRow{Slug: "cat", Source: "catalog", WindowFigures: noFigures()}
	if err := r.NAList.list(&r, nil, r.facts().code); err != nil {
		t.Fatal(err)
	}
	if r.ReasonOf("targetsCountedAllTargets") != NANotMeasured || r.ReasonOf("missingTargetsAllTargets") != NANotMeasured ||
		r.ReasonOf("partialCostPer1kAllTargets") != "" {
		t.Errorf("catalog row na %+v", r.NAList)
	}
}
