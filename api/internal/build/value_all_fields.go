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

// rangeValue is the value component of sp's range score (RankingProfile):
// the zero component, which publishes nothing, without a product or score.
func rangeValue(sp *snap.Product, rangeID string) snap.Component {
	if sp == nil {
		return snap.Component{}
	}
	return sp.Window(rangeID).Scores[RankingProfile].Components["value"]
}

// setAllTargets sets a range's 8-target bill, and when it is incomplete
// the partial bill over the targets with a cost, their count and the
// targets missing (VALUE-ALL-PARTIAL-1): the ones the range's value
// component publishes (VALUE-ALL-RANK-1), else billOver's, as
// insights.valueAll.
func (f *WindowFigures) setAllTargets(price Num, w snap.Window, cards []string, value snap.Component) {
	if a, ok := value.AllTargets(); ok {
		f.CostPer1kAllTargets, f.PartialCostPer1kAllTargets, f.CostIsLowerBound = a.Cost, a.Partial, &a.LowerBound
		f.TargetsCountedAllTargets, f.MissingTargetsAllTargets = Num(a.Counted), a.Missing
		return
	}
	b, lowerBound := billOver(price, w, cards), false
	f.CostPer1kAllTargets, f.PartialCostPer1kAllTargets, f.CostIsLowerBound = b.bill, b.partial, &lowerBound
	f.TargetsCountedAllTargets, f.MissingTargetsAllTargets = Num(b.counted), b.missing
}

// allTargetsBasis is profile.pricing.costBasisAllTargets: how the 8-target
// bill is computed and how many card targets it has, saying so when it is a
// lower bound; null without a price.
func allTargetsBasis(price Num, counted, cards int, lowerBound bool) model.Str {
	if !price.OK() {
		return nil
	}
	basis := fmt.Sprintf("Average cost per 1,000 successful requests over the %d card targets at $%s/GB "+
		"(lowest list rate; 1 GB = 1,000,000,000 bytes), counting the data of failed tries; %d of %d targets measured.",
		cards, jsnum.ToFixed(price.F(), 2), counted, cards)
	if lowerBound {
		basis += " At least this much: each target without a successful request is counted at the highest cost " +
			"per 1,000 successful requests any network of the same kind has on it."
	}
	return model.S(basis)
}

// fillPricingAllTargets sets a profile's 30d 8-target bill and its basis:
// the ones the product's 30d value component publishes, else billOver's
// over the 30d window.
func fillPricingAllTargets(pr *model.Pricing, sp *snap.Product, cards []string) {
	rate := listRate(*pr)
	if a, ok := rangeValue(sp, "30d").AllTargets(); ok {
		pr.CostPer1kAllTargets, pr.CostIsLowerBound = a.Cost, a.LowerBound
		pr.CostBasisAllTargets = allTargetsBasis(rate, a.Counted, len(cards), a.LowerBound)
		return
	}
	b := billOver(rate, sp.Window("30d"), cards)
	pr.CostPer1kAllTargets, pr.CostBasisAllTargets = b.bill, allTargetsBasis(rate, b.counted, len(cards), false)
}

// publishedValueRank is a row's valueRankAllTargets as its range's value
// component publishes it (snap.Component.Place, VALUE-ALL-RANK-1), with how
// many it ranks among; 0 for an older payload, which rankAllTargets ranks.
func publishedValueRank(sp *snap.Product, rangeID string) (*int, int) {
	value := rangeValue(sp, rangeID)
	if _, ok := value.AllTargets(); !ok {
		return nil, 0
	}
	rank, ranked, _ := value.Place()
	return rank, ranked
}

// rankAllTargets sets valueRankAllTargets on the rows of every kind none of
// whose rows has a published one (publishedValueRank): its competition rank
// among the rows of the kind with an 8-target bill, cheapest first, ties
// sharing the better rank; nil without a bill.
func rankAllTargets(rows []RankingRow) {
	byKind, published := map[string][]*RankingRow{}, map[string]bool{}
	for i := range rows {
		r := &rows[i]
		published[r.Kind] = published[r.Kind] || r.valueRanked > 0
		if r.CostPer1kAllTargets.OK() {
			byKind[r.Kind] = append(byKind[r.Kind], r)
		}
	}
	for kind, rs := range byKind {
		if published[kind] {
			continue
		}
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
