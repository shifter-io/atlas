package build_test

import (
	"encoding/json"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/build"
	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/Zeus-Development/proxy-index/api/internal/store"
	"github.com/Zeus-Development/proxy-index/api/internal/testscale"
)

func homeBody(t *testing.T, res *build.Result, rangeID string) *build.HomeAnswer {
	t.Helper()
	h, ok := res.Pages.Registered["/v1/home/"+rangeID].(*build.HomeAnswer)
	if !ok {
		t.Fatalf("no /v1/home/%s body (unbuilt: %v)", rangeID, res.Pages.Unbuilt)
	}
	return h
}

// Both committed fixtures give a home per range: the measured network's
// panel with its curve and sparks, the market's requests and curve, the
// tape's first rows, all well under the answer cap.
func TestHomeFromBothFixtures(t *testing.T) {
	t.Parallel()
	for _, name := range []string{"snapshot.json", "snapshot-every-request.json"} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			res := publishedBuild(t, fixtureSnapshot(t, name, nil))
			for _, r := range build.Ranges {
				h := homeBody(t, res, r.ID)
				if len(h.Networks) != len(res.Pages.Rankings[r.ID].Networks) || len(h.Networks) == 0 {
					t.Fatalf("%s: %d networks", r.ID, len(h.Networks))
				}
				m := h.Market
				if !m.Requests.OK() || m.Networks == 0 || m.Targets == 0 || len(m.Curve.Values) == 0 || !m.MedianSuccess.OK() {
					t.Errorf("%s: market %+v", r.ID, m)
				}
				measured := 0
				for _, n := range h.Networks {
					if len(n.Curve.Values) > 0 && n.Requests.OK() && n.LatencyP50.OK() {
						measured++
					}
				}
				if measured == 0 || len(h.Tape) > 25 || len(h.Profiles) != len(res.Dataset.Profiles) {
					t.Errorf("%s: %d measured networks, %d tape rows, %d profiles", r.ID, measured, len(h.Tape), len(h.Profiles))
				}
				checkFixtureInsights(t, r.ID, h.Insights)
				body, err := json.Marshal(h)
				if err != nil || len(body) > 20<<10 {
					t.Errorf("%s: %d bytes, %v", r.ID, len(body), err)
				}
				t.Logf("%s: %d bytes", r.ID, len(body))
			}
		})
	}
}

// The fixture's one measured network is residential: its all-targets bill is
// in the value card (priced or null with its reason), it is the authenticity and the
// exclusivity list, with no upstream (no other pool). The hardest target is a
// measured one (difficulty 1), never the catalog-only Google Search
// (difficulty 3), with the network's http rate on it.
func checkFixtureInsights(t *testing.T, rangeID string, ins build.HomeInsights) {
	t.Helper()
	v := ins.ValueAll
	if len(v.Rows) != 1 || v.Rows[0].Slug != "shifter" || (v.Best == nil) != !v.Rows[0].CostPer1k.OK() {
		t.Errorf("%s: valueAll %+v", rangeID, v)
	}
	if len(ins.Authenticity) != 1 || ins.Authenticity[0].Slug != "shifter" {
		t.Errorf("%s: authenticity %+v", rangeID, ins.Authenticity)
	}
	if len(ins.Exclusivity) != 1 || ins.Exclusivity[0].Slug != "shifter" || ins.Exclusivity[0].Upstream != nil {
		t.Errorf("%s: exclusivity %+v", rangeID, ins.Exclusivity)
	}
	h, rates := ins.Hardest, ins.HardestRates
	if h == nil || h.ID != "generic_http" || h.Difficulty != 1 || len(rates) != 1 || rates[0].Slug != "shifter" ||
		!rates[0].SuccessRate.OK() || model.SV(rates[0].Engine) != model.EngineHTTP || rates[0].Scores != nil {
		t.Errorf("%s: hardest %+v, rates %+v", rangeID, h, rates)
	}
}

// At QA-1 size (18 measured networks) and with the week's networks (the
// shape TestPageLatency serves, without its extra lists), every network
// priced and a browser target with a difficulty, so that every insight card
// is full, each body stays under the answer cap; the test logs the sizes.
func TestHomeAtQA1Size(t *testing.T) {
	t.Parallel()
	if testing.Short() {
		t.Skip("builds a QA-1-sized build")
	}
	for name, shape := range map[string]testscale.NetworkShape{"QA-1": testscale.QA1Networks, "week": testscale.Week.NetworkShape} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			in, err := testscale.NetworkInputs(shape)
			if err != nil {
				t.Fatal(err)
			}
			hard := int16(4)
			in.Targets = append(in.Targets, store.TargetRow{ID: "google_search", Difficulty: &hard})
			for i, p := range in.Providers {
				price := 1 + float64(i)/4
				in.Pricing = append(in.Pricing, store.PricingRow{ProviderSlug: p.Slug, Category: "residential", PricePerGb: &price})
			}
			res, err := build.Build(in)
			if err != nil {
				t.Fatal(err)
			}
			for _, r := range build.Ranges {
				h := homeBody(t, res, r.ID)
				body, err := json.Marshal(h)
				if err != nil || len(body) > build.MaxAnswerBytes || len(h.Networks) != len(res.Pages.Rankings[r.ID].Networks) || len(h.Networks) < 18 {
					t.Errorf("%s: %d bytes, %d networks, %v", r.ID, len(body), len(h.Networks), err)
				}
				ins := h.Insights
				if len(ins.ValueAll.Rows) == 0 || len(ins.Authenticity) != 5 || len(ins.Exclusivity) != 5 || ins.Hardest == nil || len(ins.HardestRates) == 0 {
					t.Errorf("%s: insights %+v", r.ID, ins)
				}
				t.Logf("%s: %d bytes, %d networks, %d label lists, %d hardest rates", r.ID, len(body), len(h.Networks), len(h.Labels),
					len(ins.HardestRates))
			}
		})
	}
}
