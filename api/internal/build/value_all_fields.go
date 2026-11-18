package build

import (
	"sort"

	"github.com/shifter-io/atlas/api/internal/model"
	"github.com/shifter-io/atlas/api/internal/snap"
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
	f.SuccessRateAllTargets = successRateOver(w, cards)
	if a, ok := value.AllTargets(); ok {
		f.CostPer1kAllTargets, f.PartialCostPer1kAllTargets, f.CostIsLowerBound = a.Cost, a.Partial, &a.LowerBound
		f.TargetsCountedAllTargets, f.MissingTargetsAllTargets = Num(a.Counted), a.Missing
		f.CarriedOverTargetsAllTargets = carriedOver(a)
		return
	}
	f.CarriedOverTargetsAllTargets = map[string]Num{}
	b, lowerBound := billOver(price, w, cards, nil), false
	f.CostPer1kAllTargets, f.PartialCostPer1kAllTargets, f.CostIsLowerBound = b.bill, b.partial, &lowerBound
	f.TargetsCountedAllTargets, f.MissingTargetsAllTargets = Num(b.counted), b.missing
}

// successRateOver is the success rate behind window w's bill over targets
// (SUCCESS-ALL-1, the owner's choice of 2026-11-18): the plain mean of each
// target's successes ÷ completed tries, so each target weighs as in the
// bill (total successes ÷ total tries would be the HTTP targets' alone);
// NaN when a target has no try.
func successRateOver(w snap.Window, targets []string) Num {
	if len(targets) == 0 {
		return nan
	}
	sum := 0.0
	for _, id := range targets {
		m := w.Targets[id].Metrics
		tries := countOf(m.Completed)
		if tries == 0 {
			return nan
		}
		sum += float64(countOf(m.Successes)) / float64(tries)
	}
	return Num(sum / float64(len(targets)))
}

// fillPricingAllTargets sets a profile's 30d 8-target bill: the one the
// product's 30d value component publishes, else billOver's over the 30d
// window.
func fillPricingAllTargets(pr *model.Pricing, sp *snap.Product, cards []string) {
	if a, ok := rangeValue(sp, "30d").AllTargets(); ok {
		pr.CostPer1kAllTargets, pr.CostIsLowerBound = a.Cost, a.LowerBound
		return
	}
	b := billOver(costRate(*pr), sp.Window("30d"), cards, nil)
	pr.CostPer1kAllTargets = b.bill
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
