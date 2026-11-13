package snap

import (
	"encoding/json"

	"github.com/Zeus-Development/proxy-index/api/internal/jsnum"
)

// AllTargets is the 8-target bill the value component publishes with its
// rank (VALUE-ALL-RANK-1, [value] basis "rank_all_targets"): the cost per
// 1,000 successes over the card targets (a lower bound when LowerBound),
// the partial bill over the targets with a cost when some, not all, have
// one, how many have one, and the targets missing one for a measured
// reason, in card order. Costs are NaN when null.
type AllTargets struct {
	Cost, Partial Num
	Counted       int
	Missing       []string
	// LowerBound is true when a target without successes is costed at the
	// highest cost per 1,000 any network of the kind has on it in the window.
	LowerBound bool
}

// AllTargets is the component's published 8-target bill; published is false
// without inputs.cost_is_lower_bound (a payload from before
// VALUE-ALL-RANK-1, or a component that is not the value one).
func (c Component) AllTargets() (a AllTargets, published bool) {
	var in struct {
		Cost       *float64 `json:"cost_per_1k_all_targets"`
		Partial    *float64 `json:"partial_cost_per_1k_all_targets"`
		Counted    *float64 `json:"targets_counted_all_targets"`
		Missing    []string `json:"missing_targets_all_targets"`
		LowerBound *bool    `json:"cost_is_lower_bound"`
	}
	if len(c.Inputs) == 0 || json.Unmarshal(c.Inputs, &in) != nil || in.LowerBound == nil {
		return AllTargets{Cost: jsnum.NaN, Partial: jsnum.NaN, Missing: []string{}}, false
	}
	a = AllTargets{Cost: numOf(in.Cost), Partial: numOf(in.Partial), Missing: in.Missing, LowerBound: *in.LowerBound}
	if in.Counted != nil {
		a.Counted = int(*in.Counted)
	}
	if a.Missing == nil {
		a.Missing = []string{}
	}
	return a, true
}

// numOf is a published number, NaN when null.
func numOf(v *float64) Num {
	if v == nil {
		return jsnum.NaN
	}
	return Num(*v)
}
