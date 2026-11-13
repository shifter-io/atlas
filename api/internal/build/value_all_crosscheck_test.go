package build

import (
	"encoding/json"
	"math"
	"os"
	"path/filepath"
	"slices"
	"testing"

	"github.com/Zeus-Development/atlas/api/internal/snap"
)

// valueAllCase is one case of testdata/value_all_crosscheck.json, written
// by the calculation's port of billOver (VALUE-ALL-RANK-1): a price (null:
// unpriced), each target's window metrics and the calculation's figures.
type valueAllCase struct {
	Name     string                  `json:"name"`
	Price    *float64                `json:"price"`
	Metrics  map[string]snap.Metrics `json:"metrics"`
	Expected struct {
		Cost    *float64 `json:"cost"`
		Partial *float64 `json:"partial"`
		Counted int      `json:"counted"`
		Missing []string `json:"missing"`
	} `json:"expected"`
}

// The calculation's 8-target bill is billOver's: the same bill, partial
// bill, counted and missing targets on every case of the shared fixture, to
// 1e-9 (null is NaN).
func TestValueAllCrossCheck(t *testing.T) {
	t.Parallel()
	raw, err := os.ReadFile(filepath.Join("testdata", "value_all_crosscheck.json"))
	if err != nil {
		t.Fatal(err)
	}
	var fx struct {
		Targets []string       `json:"targets"`
		Cases   []valueAllCase `json:"cases"`
	}
	if err := json.Unmarshal(raw, &fx); err != nil {
		t.Fatal(err)
	}
	if len(fx.Targets) != 8 || len(fx.Cases) == 0 {
		t.Fatalf("%d targets, %d cases", len(fx.Targets), len(fx.Cases))
	}
	for _, c := range fx.Cases {
		w := snap.Window{Targets: map[string]snap.TargetResult{}}
		for id, m := range c.Metrics {
			w.Targets[id] = snap.TargetResult{Metrics: m}
		}
		b, want := billOver(numOrNaN(c.Price), w, fx.Targets), c.Expected
		if !sameFigure(b.bill, want.Cost) || !sameFigure(b.partial, want.Partial) || b.counted != want.Counted ||
			!slices.Equal(b.missing, want.Missing) {
			t.Errorf("%s: Go bill %v partial %v counted %d missing %v, calculation %+v", c.Name, b.bill, b.partial,
				b.counted, b.missing, want)
		}
	}
}

func numOrNaN(v *float64) Num {
	if v == nil {
		return nan
	}
	return Num(*v)
}

// sameFigure is whether got equals want to 1e-9 (relative above 1), null
// being NaN.
func sameFigure(got Num, want *float64) bool {
	if want == nil {
		return !got.OK()
	}
	return got.OK() && math.Abs(got.F()-*want) <= 1e-9*math.Max(1, math.Abs(*want))
}
