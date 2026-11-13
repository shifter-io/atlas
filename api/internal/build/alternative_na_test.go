package build

import (
	"fmt"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/store"
	"github.com/Zeus-Development/proxy-index/api/internal/testscale"
)

// pricedWeekInputs is the week fixture with every network but net00 priced,
// net01 the cheapest, and net02's targets without wire bytes, so its
// all-targets bill is incomplete.
func pricedWeekInputs(t *testing.T) func(testscale.Shape) (*store.Inputs, error) {
	return func(shape testscale.Shape) (*store.Inputs, error) {
		in, err := testscale.WeekInputs(shape)
		if err != nil {
			return nil, err
		}
		for n := 1; n < shape.Networks; n++ {
			price := float64(n)
			in.Pricing = append(in.Pricing, store.PricingRow{ProviderSlug: fmt.Sprintf("net%02d", n), Category: "residential", PricePerGb: &price})
		}
		in.Snapshot = editSnapshot(t, in.Snapshot, func(doc map[string]any) {
			for _, p := range doc["products"].([]any) {
				if p := p.(map[string]any); p["provider"] == "net02" {
					for _, w := range p["windows"].(map[string]any) {
						for _, tg := range w.(map[string]any)["targets"].(map[string]any) {
							m := tg.(map[string]any)["metrics"].(map[string]any)
							m["bytes_up"], m["bytes_down"] = 0, 0
						}
					}
				}
			}
		})
		return in, nil
	}
}

// ALT-REASON-1: a null alternative has its own reason, never the generic
// insufficient_data: no_cheaper_peer for the cheapest network, and
// incomplete_targets for one without an all-targets bill; the walker of
// TestEveryPhase3AnswerListsItsNulls finds every null listed.
func TestEveryNullAlternativeHasItsReason(t *testing.T) {
	t.Parallel()
	pg := targetsScaleBuild(t, pricedWeekInputs(t)).Pages
	want := map[string]string{"net00": NACatalog, "net01": NANoCheaperPeer, "net02": NAIncompleteTargets}
	for _, r := range Ranges {
		for slug, code := range want {
			st := pg.Network[slug+"/residential"].Stats[r.ID]
			if got := st.ReasonOf("alternative"); st.Alternative != nil || got != code {
				t.Errorf("%s %s: alternative %+v reason %q, want %s", r.ID, slug, st.Alternative, got, code)
			}
		}
		if st := pg.Network["net03/residential"].Stats[r.ID]; st.Alternative == nil || st.Alternative.Slug != "net01" {
			t.Errorf("%s net03 alternative %+v", r.ID, st.Alternative)
		}
	}
	for k, a := range pg.Network {
		for _, p := range naProblems(t, a, nil) {
			t.Errorf("network %s: %s", k, p)
		}
		for rangeID, st := range a.Stats {
			if st.Alternative == nil && st.ReasonOf("alternative") == NAInsufficient {
				t.Errorf("network %s %s: alternative has the generic reason", k, rangeID)
			}
		}
	}
}

// A missing price or run keeps its reason before the alternative's own.
func TestTheAlternativeReasonComesAfterPriceAndRun(t *testing.T) {
	t.Parallel()
	s := &RangeStats{altWhy: NANoCheaperPeer}
	ran := naFacts{measured: true, ran: true, priced: true}
	for _, c := range []struct {
		f          naFacts
		path, want string
	}{
		{ran, "alternative", NANoCheaperPeer},
		{naFacts{measured: true, ran: true}, "alternative", NACatalog},
		{naFacts{measured: true, priced: true}, "alternative", NANoRun},
		{ran, "captchaRate", NAInsufficient},
	} {
		if got := s.altCode(c.f.code)(c.path); got != c.want {
			t.Errorf("%+v %s: %s, want %s", c.f, c.path, got, c.want)
		}
	}
	if got := (&RangeStats{}).altCode(ran.code)("alternative"); got != NAInsufficient {
		t.Errorf("without its own reason: %s", got)
	}
}
