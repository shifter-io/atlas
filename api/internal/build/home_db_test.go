package build_test

import (
	"encoding/json"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/build"
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
					if len(n.Curve.Values) > 0 && n.Requests.OK() && n.LatencyNote != nil {
						measured++
					}
				}
				if measured == 0 || len(h.Tape) > 25 || len(h.Profiles) != len(res.Dataset.Profiles) {
					t.Errorf("%s: %d measured networks, %d tape rows, %d profiles", r.ID, measured, len(h.Tape), len(h.Profiles))
				}
				body, err := json.Marshal(h)
				if err != nil || len(body) > 20<<10 {
					t.Errorf("%s: %d bytes, %v", r.ID, len(body), err)
				}
				t.Logf("%s: %d bytes", r.ID, len(body))
			}
		})
	}
}

// At QA-1 size (18 measured networks) each body stays under the answer cap; the
// test logs the sizes.
func TestHomeAtQA1Size(t *testing.T) {
	t.Parallel()
	if testing.Short() {
		t.Skip("builds a QA-1-sized build")
	}
	in, err := testscale.NetworkInputs(testscale.QA1Networks)
	if err != nil {
		t.Fatal(err)
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
		t.Logf("%s: %d bytes, %d networks, %d label lists", r.ID, len(body), len(h.Networks), len(h.Labels))
	}
}
