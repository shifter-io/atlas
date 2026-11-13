package build

import (
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/model"
)

// publishValueRanks gives each listed network's balanced value component of
// every window a published rank (nil: not ranked) among ranked networks, as
// the framework does with [value] basis "rank" (VALUE-BY-RANK-1).
func publishValueRanks(doc map[string]any, ranks map[string]any, ranked float64) {
	for _, p := range doc["products"].([]any) {
		p := p.(map[string]any)
		rank, ok := ranks[p["provider"].(string)]
		if !ok {
			continue
		}
		for _, w := range p["windows"].(map[string]any) {
			s := w.(map[string]any)["scores"].(map[string]any)["balanced"].(map[string]any)
			v := s["components"].(map[string]any)["value"].(map[string]any)
			v["inputs"] = map[string]any{"value": 1.0, "cost_per_1k": 0.5, "rank": rank, "ranked": ranked, "peers": 3.0}
		}
	}
}

// The rankings' valueRank, and the network answer's ranks.valueRank, are the
// rank the snapshot publishes with the value component, not the API's own
// cost board, so the value bar and the rank never disagree.
func TestValueRankIsThePublishedRank(t *testing.T) {
	t.Parallel()
	// The opposite of any cost order: net00 third, net02 first, net01 unranked.
	published := map[string]any{"net00": 3.0, "net01": nil, "net02": 1.0}
	res, _ := extrasBuild(t, func(doc map[string]any) { publishValueRanks(doc, published, 7) })
	for _, r := range Ranges {
		for _, row := range res.Pages.RankingsByKind[r.ID+"/residential"].Networks {
			want, ok := published[row.Slug]
			if !ok {
				continue
			}
			ans := res.Pages.Network[row.Slug+"/residential"]
			got := ans.Stats[r.ID].Ranks.ValueRank
			if want == nil {
				if row.ValueRank != nil || got.Rank.OK() || got.Peers != 7 {
					t.Errorf("%s %s valueRank %v, ranks %+v, want null of 7", r.ID, row.Slug, row.ValueRank, got)
				}
				continue
			}
			rank := int(want.(float64))
			if row.ValueRank == nil || *row.ValueRank != rank || got != (model.Rank{Rank: Num(rank), Peers: 7}) {
				t.Errorf("%s %s valueRank %v, ranks %+v, want %d of 7", r.ID, row.Slug, row.ValueRank, got, rank)
			}
		}
	}
}

// Without a published rank (value scored by ratio, an older snapshot) the
// rankings keep the cost board's place.
func TestValueRankWithoutAPublishedRankIsTheCostBoards(t *testing.T) {
	t.Parallel()
	res, _ := extrasBuild(t, nil)
	rows := res.Pages.RankingsByKind["30d/residential"].Networks
	for _, row := range rows {
		if row.valueRanked != 0 {
			t.Errorf("%s published count %d without a published rank", row.Slug, row.valueRanked)
		}
		if row.CostPer1k.OK() != (row.ValueRank != nil) {
			t.Errorf("%s cost %v, valueRank %v", row.Slug, row.CostPer1k, row.ValueRank)
		}
	}
}
