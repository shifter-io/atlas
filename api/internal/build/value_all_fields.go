package build

import (
	"fmt"
	"sort"

	"github.com/Zeus-Development/proxy-index/api/internal/jsnum"
	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/Zeus-Development/proxy-index/api/internal/snap"
)

// VALUE-ALL-2: the 8-target bill (VALUE-ALL-1's targetCost, averaged with
// equal weights over the card targets) on the rankings, the home market, the
// network pages and the dataset.

// allTargetsBill is window w's bill per 1,000 successes over the card targets
// at price: their costs' plain mean, NaN unless every one has a cost (no
// price, or a target missing). counted is how many targets have a cost.
func allTargetsBill(price Num, w snap.Window, cards []string) (bill Num, counted int) {
	sum := 0.0
	for _, id := range cards {
		if tc, why := targetCost(price, w.Targets[id].Metrics); why == "" {
			sum, counted = sum+tc.CostPer1k.F(), counted+1
		}
	}
	if len(cards) == 0 || counted < len(cards) {
		return nan, counted
	}
	return Num(sum / float64(len(cards))), counted
}

// allTargetsBasis is profile.pricing.costBasisAllTargets: how the 8-target
// bill is computed and how many card targets it has; null without a price.
func allTargetsBasis(price Num, counted, cards int) model.Str {
	if !price.OK() {
		return nil
	}
	return model.S(fmt.Sprintf("Average cost per 1,000 successful requests over the %d card targets at $%s/GB "+
		"(lowest list rate; 1 GB = 1,000,000,000 bytes), counting the data of failed tries; %d of %d targets measured.",
		cards, jsnum.ToFixed(price.F(), 2), counted, cards))
}

// fillPricingAllTargets sets a profile's 30d 8-target bill and its basis
// from the product's 30d window.
func fillPricingAllTargets(pr *model.Pricing, w snap.Window, cards []string) {
	rate := listRate(*pr)
	bill, counted := allTargetsBill(rate, w, cards)
	pr.CostPer1kAllTargets, pr.CostBasisAllTargets = bill, allTargetsBasis(rate, counted, len(cards))
}

// rankAllTargets sets each row's valueRankAllTargets: its competition rank
// among the rows of its kind with an 8-target bill, cheapest first, ties
// sharing the better rank; nil without a bill.
func rankAllTargets(rows []RankingRow) {
	byKind := map[string][]*RankingRow{}
	for i := range rows {
		if r := &rows[i]; r.CostPer1kAllTargets.OK() {
			byKind[r.Kind] = append(byKind[r.Kind], r)
		}
	}
	for _, rs := range byKind {
		sort.SliceStable(rs, func(i, j int) bool { return rs[i].CostPer1kAllTargets < rs[j].CostPer1kAllTargets })
		for i, r := range rs {
			rank := i + 1
			if i > 0 && r.CostPer1kAllTargets == rs[i-1].CostPer1kAllTargets {
				rank = *rs[i-1].ValueRankAllTargets
			}
			r.ValueRankAllTargets = &rank
		}
	}
}

// homeSpreadAll is market.spreadAll: valueAll's spread with its best and
// worst network; nil without a complete priced row.
func homeSpreadAll(v HomeValueAll) *HomeSpread {
	if v.Best == nil || v.Worst == nil {
		return nil
	}
	return &HomeSpread{Value: v.SpreadX, Best: &HomeRef{v.Best.Slug, v.Best.Name}, Worst: &HomeRef{v.Worst.Slug, v.Worst.Name}}
}

// cardTargets is the ids of the card targets (isCard), in dataset order.
func cardTargets(ts []model.TargetMeta) []string {
	out := []string{}
	for _, t := range ts {
		if isCard(t) {
			out = append(out, t.ID)
		}
	}
	return out
}
