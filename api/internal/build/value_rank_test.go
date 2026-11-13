package build

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/Zeus-Development/atlas/api/internal/model"
)

// publishValueAll gives each listed network's balanced value component of
// every window the 8-target bill (nil: unpriced) and a published rank (nil:
// not ranked) among ranked networks, as the framework does with [value]
// basis "rank_all_targets" (VALUE-ALL-RANK-1).
func publishValueAll(doc map[string]any, bills, ranks map[string]any, ranked float64) {
	for _, p := range doc["products"].([]any) {
		p := p.(map[string]any)
		rank, ok := ranks[p["provider"].(string)]
		if !ok {
			continue
		}
		bill := bills[p["provider"].(string)]
		for _, w := range p["windows"].(map[string]any) {
			s := w.(map[string]any)["scores"].(map[string]any)["balanced"].(map[string]any)
			v := s["components"].(map[string]any)["value"].(map[string]any)
			v["inputs"] = map[string]any{"value": 1.0, "cost_per_1k_all_targets": bill, "partial_cost_per_1k_all_targets": nil,
				"targets_counted_all_targets": 8, "missing_targets_all_targets": []string{}, "cost_is_lower_bound": rank == 1.0,
				"rank": rank, "ranked": ranked, "peers": 3.0}
		}
	}
}

// The rankings' valueRankAllTargets and bill, and the network answer's
// ranks.valueRankAllTargets, are the ones the snapshot publishes with the
// value component, not the API's own, so the value bar and the rank never
// disagree; the plain-HTTP valueRank is gone.
func TestValueRankAllTargetsIsThePublishedRank(t *testing.T) {
	t.Parallel()
	// The opposite of any cost order: net00 third, net02 first (a lower
	// bound), net01 unpriced.
	published := map[string]any{"net00": 3.0, "net01": nil, "net02": 1.0}
	bills := map[string]any{"net00": 0.5, "net01": nil, "net02": 4.0}
	res, _ := extrasBuild(t, func(doc map[string]any) { publishValueAll(doc, bills, published, 7) })
	for _, r := range Ranges {
		ranking := res.Pages.RankingsByKind[r.ID+"/residential"]
		if raw, err := json.Marshal(ranking); err != nil || strings.Contains(string(raw), `"valueRank"`) {
			t.Errorf("%s rankings serve valueRank (%v)", r.ID, err)
		}
		for _, row := range ranking.Networks {
			want, ok := published[row.Slug]
			if !ok {
				continue
			}
			got := res.Pages.Network[row.Slug+"/residential"].Stats[r.ID].Ranks.ValueRankAllTargets
			if bill, _ := bills[row.Slug].(float64); row.CostPer1kAllTargets.OK() != (bill > 0) ||
				(bill > 0 && row.CostPer1kAllTargets != Num(bill)) || row.CostIsLowerBound == nil || *row.CostIsLowerBound != (want == 1.0) {
				t.Errorf("%s %s bill %v lower %v, want %v", r.ID, row.Slug, row.CostPer1kAllTargets, isTrue(row.CostIsLowerBound), bills[row.Slug])
			}
			if want == nil {
				if row.ValueRankAllTargets != nil || got.Rank.OK() || got.Peers != 7 {
					t.Errorf("%s %s valueRankAllTargets %v, ranks %+v, want null of 7", r.ID, row.Slug, row.ValueRankAllTargets, got)
				}
				continue
			}
			rank := int(want.(float64))
			if row.ValueRankAllTargets == nil || *row.ValueRankAllTargets != rank || got != (model.Rank{Rank: Num(rank), Peers: 7}) {
				t.Errorf("%s %s valueRankAllTargets %v, ranks %+v, want %d of 7", r.ID, row.Slug, row.ValueRankAllTargets, got, rank)
			}
		}
	}
	if pr := res.Pages.Network["net02/residential"].Profile.Pricing; pr.CostPer1kAllTargets != 4 || !pr.CostIsLowerBound {
		t.Errorf("net02 profile pricing %v lower %v", pr.CostPer1kAllTargets, pr.CostIsLowerBound)
	}
}

// Without a published bill (a snapshot from before VALUE-ALL-RANK-1) the
// rankings keep the API's own 8-target bill and rank.
func TestValueRankAllTargetsWithoutAPublishedBillIsTheAPIs(t *testing.T) {
	t.Parallel()
	res, _ := extrasBuild(t, nil)
	rows := res.Pages.RankingsByKind["30d/residential"].Networks
	for _, row := range rows {
		if row.valueRanked != 0 || isTrue(row.CostIsLowerBound) {
			t.Errorf("%s published count %d, lower bound %v without a published bill", row.Slug, row.valueRanked, isTrue(row.CostIsLowerBound))
		}
		if row.CostPer1kAllTargets.OK() != (row.ValueRankAllTargets != nil) {
			t.Errorf("%s bill %v, valueRankAllTargets %v", row.Slug, row.CostPer1kAllTargets, row.ValueRankAllTargets)
		}
	}
}
