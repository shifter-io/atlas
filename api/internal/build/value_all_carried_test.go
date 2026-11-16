package build

import (
	"encoding/json"
	"maps"
	"testing"

	"github.com/Zeus-Development/atlas/api/internal/snap"
)

// vaCarried is a network whose 24h window has no amazon_pdp run and whose 7d
// window has every card target, its browser targets at twice the 24h cost;
// its 24h value component is published with inputs.
func vaCarried(t *testing.T, inputs map[string]any) *entry {
	t.Helper()
	e := vaEntry("late", 2, 1, nil)
	sp := e.Snap[valueAllKind]
	day, week := sp.Windows["30d"], vaEntry("week", 2, 2, nil).Snap[valueAllKind].Windows["30d"]
	day.Targets, week.Targets = maps.Clone(day.Targets), maps.Clone(week.Targets)
	for id := range week.Targets {
		if id != "generic_http" && id != "generic_https" {
			for _, w := range []snap.Window{day, week} {
				if r, ok := w.Targets[id]; ok {
					r.Browser = &snap.BrowserResult{}
					w.Targets[id] = r
				}
			}
		}
	}
	delete(day.Targets, "amazon_pdp")
	raw, err := json.Marshal(inputs)
	if err != nil {
		t.Fatal(err)
	}
	day.Scores = map[string]snap.Score{RankingProfile: {Components: map[string]snap.Component{"value": {Inputs: raw}}}}
	sp.Windows = map[string]snap.Window{"24h": day, "7d": week}
	return e
}

func vaCarriedInputs(window any, carried map[string]float64) map[string]any {
	return map[string]any{"value": 2, "cost_per_1k_all_targets": 1.5, "partial_cost_per_1k_all_targets": nil,
		"targets_counted_all_targets": 8, "missing_targets_all_targets": []string{}, "cost_is_lower_bound": false,
		"browser_window": window, "carried_over_all_targets": carried, "rank": 1, "ranked": 1, "peers": 1}
}

// COVERAGE-ALERT-1: with [value] browser_window the 24h bill costs its
// browser targets over the 7d window: byTarget follows it (amazon_pdp, with
// no 24h run, costed and not no_run), the HTTP targets keep their 24 hours,
// and the carried-over targets are served with their age.
func TestACarriedOverTargetIsCostedFromTheBrowserWindow(t *testing.T) {
	t.Parallel()
	e := vaCarried(t, vaCarriedInputs("7d", map[string]float64{"amazon_pdp": 72}))
	row := homeValueAll(vaContext(e), vaRows("late"), "24h").Rows[0]
	if row.CostPer1k != 1.5 || len(row.MissingTargets) != 0 || len(row.CarriedOverTargets) != 1 || row.CarriedOverTargets["amazon_pdp"] != 72 {
		t.Errorf("row %+v", row)
	}
	if got := row.ByTarget["amazon_pdp"].CostPer1k; !vaNear(got, 2) {
		t.Errorf("amazon_pdp costed %v, want the 7d window's 2", got)
	}
	if got := row.ByTarget["generic_http"].CostPer1k; !vaNear(got, 1) {
		t.Errorf("generic_http costed %v, want its own 24 hours' 1", got)
	}
	for _, p := range row.NA {
		if p == "byTarget.amazon_pdp.costPer1k" {
			t.Errorf("a carried-over target lists %s in its na", p)
		}
	}
	e.P.Pricing = e.P.ProductMeasurements.Get(valueAllKind).Pricing
	ws := windowStats(e.P, e.Snap[valueAllKind], "24h", vaCards)
	if c := ws.CarriedOverTargetsAllTargets; len(c) != 1 || c["amazon_pdp"] != 72 {
		t.Errorf("carriedOverTargetsAllTargets %v", c)
	}
}

// A payload without browser_window (before COVERAGE-ALERT-1): every target
// is costed over the range's own window and nothing is carried over.
func TestWithoutABrowserWindowNothingIsCarriedOver(t *testing.T) {
	t.Parallel()
	inputs := vaCarriedInputs(nil, nil)
	delete(inputs, "browser_window")
	delete(inputs, "carried_over_all_targets")
	e := vaCarried(t, inputs)
	row := homeValueAll(vaContext(e), vaRows("late"), "24h").Rows[0]
	if row.CarriedOverTargets == nil || len(row.CarriedOverTargets) != 0 || row.ByTarget["amazon_pdp"].CostPer1k.OK() {
		t.Errorf("row %+v", row)
	}
	e.P.Pricing = e.P.ProductMeasurements.Get(valueAllKind).Pricing
	ws := windowStats(e.P, e.Snap[valueAllKind], "24h", vaCards)
	if c := ws.CarriedOverTargetsAllTargets; c == nil || len(c) != 0 {
		t.Errorf("carriedOverTargetsAllTargets %v", c)
	}
}
