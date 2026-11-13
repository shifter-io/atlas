package snap

import (
	"encoding/json"
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
