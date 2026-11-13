package snap

import (
	"encoding/json"
	"slices"
	"testing"
)

// A component scored by place publishes its rank and how many it ranks
// among (VALUE-BY-RANK-1); one scored by ratio, or an older bare number,
// publishes none.
func TestComponentPlace(t *testing.T) {
	for _, c := range []struct {
		name, inputs string
		rank, ranked int // rank 0: nil
		published    bool
	}{
		{"ranked", `{"value":2.5,"cost_per_1k":0.4,"rank":3,"ranked":18,"peers":17}`, 3, 18, true},
		{"not ranked", `{"value":null,"cost_per_1k":null,"rank":null,"ranked":18,"peers":17}`, 0, 18, true},
		{"by ratio", `{"value":2.5,"best":1,"peers":17}`, 0, 0, false},
		{"bare number", `2.5`, 0, 0, false},
		{"none", ``, 0, 0, false},
	} {
		rank, ranked, published := Component{Inputs: json.RawMessage(c.inputs)}.Place()
		if published != c.published || ranked != c.ranked || (rank == nil) != (c.rank == 0) || (rank != nil && *rank != c.rank) {
			t.Errorf("%s: rank %v of %d published %v", c.name, rank, ranked, published)
		}
	}
}

// The value component publishes its 8-target bill with its rank
// (VALUE-ALL-RANK-1); a payload without cost_is_lower_bound publishes none.
func TestComponentAllTargets(t *testing.T) {
	for _, c := range []struct {
		name, inputs     string
		cost, partial    float64 // -1: null
		counted          int
		missing          []string
		lower, published bool
	}{
		{"complete", `{"value":2.5,"cost_per_1k_all_targets":1.25,"partial_cost_per_1k_all_targets":null,` +
			`"targets_counted_all_targets":8,"missing_targets_all_targets":[],"cost_is_lower_bound":false,"rank":3,"ranked":18}`,
			1.25, -1, 8, []string{}, false, true},
		{"lower bound", `{"cost_per_1k_all_targets":4.5,"partial_cost_per_1k_all_targets":2,"targets_counted_all_targets":6,` +
			`"missing_targets_all_targets":["google_search","cloudflare_challenge"],"cost_is_lower_bound":true}`,
			4.5, 2, 6, []string{"google_search", "cloudflare_challenge"}, true, true},
		{"unpriced", `{"cost_per_1k_all_targets":null,"targets_counted_all_targets":0,"cost_is_lower_bound":false}`,
			-1, -1, 0, []string{}, false, true},
		{"before", `{"value":2.5,"cost_per_1k":0.4,"rank":3,"ranked":18,"peers":17}`, -1, -1, 0, []string{}, false, false},
		{"bare number", `2.5`, -1, -1, 0, []string{}, false, false},
		{"none", ``, -1, -1, 0, []string{}, false, false},
	} {
		a, published := Component{Inputs: json.RawMessage(c.inputs)}.AllTargets()
		same := func(got Num, want float64) bool { return (want < 0 && !got.OK()) || float64(got) == want }
		if published != c.published || !same(a.Cost, c.cost) || !same(a.Partial, c.partial) || a.Counted != c.counted ||
			a.LowerBound != c.lower || !slices.Equal(a.Missing, c.missing) || a.Missing == nil {
			t.Errorf("%s: %+v published %v", c.name, a, published)
		}
	}
}
