package build

import (
	"encoding/json"
	"slices"
	"strings"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/Zeus-Development/proxy-index/api/internal/snap"
)

// vaPublish gives e's 30d balanced score a value component with inputs, as
// the calculation publishes it.
func vaPublish(t *testing.T, e *entry, inputs map[string]any) {
	t.Helper()
	raw, err := json.Marshal(inputs)
	if err != nil {
		t.Fatal(err)
	}
	sp := e.Snap[valueAllKind]
	w := sp.Windows["30d"]
	w.Scores = map[string]snap.Score{RankingProfile: {Components: map[string]snap.Component{"value": {Inputs: raw}}}}
	sp.Windows["30d"] = w
}

// isTrue and isFalse are whether a nullable flag is set and true, set and false.
func isTrue(b *bool) bool  { return b != nil && *b }
func isFalse(b *bool) bool { return b != nil && !*b }

// vaPublishedRow is e's 30d ranking row with its published value rank, as
// rankingRow sets it.
func vaPublishedRow(t *testing.T, e *entry) RankingRow {
	t.Helper()
	row := vaRankingRow(t, e)
	row.ValueRankAllTargets, row.valueRanked = publishedValueRank(e.Snap[valueAllKind], "30d")
	return row
}

// The calculation's 8-target figures and rank (VALUE-ALL-RANK-1) replace the
// API's on the rankings rows, their ranks, insights.valueAll and the
// profile: a lower bound is priced, ranked by it and says so; byTarget stays
// the API's per-target detail.
func TestThePublishedAllTargetsFiguresAreServed(t *testing.T) {
	t.Parallel()
	missing := []string{"google_search", "cloudflare_challenge"}
	gaps := map[string]snap.Metrics{"google_search": vaMetrics(100, 0, 1e8), "cloudflare_challenge": vaMetrics(100, 0, 2e8)}
	c := vaContext(vaEntry("lb", 2, 0.5, gaps), vaEntry("full", 2, 1, nil))
	vaPublish(t, c.Entries[0], map[string]any{"value": 2, "cost_per_1k_all_targets": 9.5, "partial_cost_per_1k_all_targets": 0.5,
		"targets_counted_all_targets": 6, "missing_targets_all_targets": missing, "cost_is_lower_bound": true, "rank": 2, "ranked": 2, "peers": 2})
	vaPublish(t, c.Entries[1], map[string]any{"value": 2, "cost_per_1k_all_targets": 1.25, "partial_cost_per_1k_all_targets": nil,
		"targets_counted_all_targets": 8, "missing_targets_all_targets": []string{}, "cost_is_lower_bound": false, "rank": 1, "ranked": 2, "peers": 2})
	ans := &RankingsAnswer{Networks: []RankingRow{vaPublishedRow(t, c.Entries[0]), vaPublishedRow(t, c.Entries[1])}}
	rankAllTargets(ans.Networks)
	if err := ans.listNA(); err != nil {
		t.Fatal(err)
	}
	lb, full := ans.Networks[0], ans.Networks[1]
	if lb.CostPer1kAllTargets != 9.5 || !isTrue(lb.CostIsLowerBound) || lb.PartialCostPer1kAllTargets != 0.5 || lb.TargetsCountedAllTargets != 6 ||
		!slices.Equal(lb.MissingTargetsAllTargets, missing) || lb.ValueRankAllTargets == nil || *lb.ValueRankAllTargets != 2 {
		t.Errorf("lower-bound row %+v rank %v", lb.WindowFigures, lb.ValueRankAllTargets)
	}
	if full.CostPer1kAllTargets != 1.25 || !isFalse(full.CostIsLowerBound) || full.ValueRankAllTargets == nil || *full.ValueRankAllTargets != 1 {
		t.Errorf("complete row %+v rank %v", full.WindowFigures, full.ValueRankAllTargets)
	}
	for _, p := range lb.NA {
		if strings.Contains(p, "AllTargets") {
			t.Errorf("a lower-bound row lists %s in its na", p)
		}
	}
	if r := rowRanks(ans.Networks); r["lb"].ValueRankAllTargets != (model.Rank{Rank: 2, Peers: 2}) {
		t.Errorf("ranks %+v", r["lb"])
	}
	v := homeValueAll(c, vaRows("lb", "full"), "30d")
	row := v.Rows[1]
	if v.Rows[0].Slug != "full" || v.Rows[0].CostPer1k != 1.25 || row.CostPer1k != 9.5 || !row.CostIsLowerBound || row.PartialCostPer1k != 0.5 ||
		row.TargetsCounted != 6 || !slices.Equal(row.MissingTargets, missing) || row.ByTarget["google_search"].CostPer1k.OK() ||
		v.Incomplete != 0 || v.Worst == nil || v.Worst.Slug != "lb" {
		t.Errorf("valueAll %+v rows %+v", v, v.Rows)
	}
	pr := model.Pricing{PricePerGb: 2, PricePerGbAtScale: nan, BasisPerGb: 2}
	fillPricingAllTargets(&pr, c.Entries[0].Snap[valueAllKind], vaCards)
	if pr.CostPer1kAllTargets != 9.5 || !pr.CostIsLowerBound {
		t.Errorf("profile pricing %v %v", pr.CostPer1kAllTargets, pr.CostIsLowerBound)
	}
}

// A value component from before VALUE-ALL-RANK-1 (its rank the plain-HTTP
// cost's) leaves the API's own bill and rank in place.
func TestAnOlderValueComponentKeepsTheAPIsBillAndRank(t *testing.T) {
	t.Parallel()
	e := vaEntry("old", 2, 1, nil)
	vaPublish(t, e, map[string]any{"value": 2, "cost_per_1k": 0.5, "rank": 5, "ranked": 7, "peers": 7})
	rows := []RankingRow{vaPublishedRow(t, e)}
	if rows[0].ValueRankAllTargets != nil || rows[0].valueRanked != 0 {
		t.Fatalf("an older rank is read as the 8-target one: %v of %d", rows[0].ValueRankAllTargets, rows[0].valueRanked)
	}
	rankAllTargets(rows)
	r := rows[0]
	if !vaNear(r.CostPer1kAllTargets, 1) || !isFalse(r.CostIsLowerBound) || r.ValueRankAllTargets == nil || *r.ValueRankAllTargets != 1 {
		t.Errorf("older payload: bill %v lower %v rank %v", r.CostPer1kAllTargets, isTrue(r.CostIsLowerBound), r.ValueRankAllTargets)
	}
	if got := rowRanks(rows)["old"].ValueRankAllTargets; got != (model.Rank{Rank: 1, Peers: 1}) {
		t.Errorf("ranks %+v", got)
	}
	pr := model.Pricing{PricePerGb: 2, PricePerGbAtScale: nan, BasisPerGb: 2}
	fillPricingAllTargets(&pr, e.Snap[valueAllKind], vaCards)
	if !vaNear(pr.CostPer1kAllTargets, 1) || pr.CostIsLowerBound {
		t.Errorf("profile pricing %v %v", pr.CostPer1kAllTargets, pr.CostIsLowerBound)
	}
}

// rankAllTargets leaves a kind with a published rank to the calculation.
func TestRankAllTargetsLeavesAPublishedKindAlone(t *testing.T) {
	t.Parallel()
	row := func(slug, kind string, bill float64, published int) RankingRow {
		r := RankingRow{Slug: slug, Kind: kind, valueRanked: published}
		r.CostPer1kAllTargets = Num(bill)
		if published > 0 {
			rank := 2
			r.ValueRankAllTargets = &rank
		}
		return r
	}
	rows := []RankingRow{row("pub", "residential", 3, 3), row("unscored", "residential", 1, 0), row("dc", "datacenter", 9, 0)}
	rankAllTargets(rows)
	if *rows[0].ValueRankAllTargets != 2 || rows[1].ValueRankAllTargets != nil || rows[2].ValueRankAllTargets == nil ||
		*rows[2].ValueRankAllTargets != 1 {
		t.Errorf("ranks %v %v %v", rows[0].ValueRankAllTargets, rows[1].ValueRankAllTargets, rows[2].ValueRankAllTargets)
	}
}
