package build

import (
	"encoding/json"
	"math"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/testscale"
)

// trafficPages builds the browser scale fixture with its window totals the
// HTTP targets' sum (as the calculator's: browser runs never enter them),
// net00's amazon_pdp without a run in any window and its google_search
// without recorded bytes.
func trafficPages(t *testing.T) *Pages {
	t.Helper()
	in, err := testscale.Inputs(testscale.Shape{Networks: 3, Countries: 1, Exits: 3, Tail: 5, Unmeasured: 1, Browser: true})
	if err != nil {
		t.Fatal(err)
	}
	in.Snapshot = editSnapshot(t, in.Snapshot, func(doc map[string]any) {
		for _, p := range doc["products"].([]any) {
			p := p.(map[string]any)
			for _, w := range p["windows"].(map[string]any) {
				w := w.(map[string]any)
				targets := w["targets"].(map[string]any)
				m := w["metrics"].(map[string]any)
				for _, k := range []string{"completed", "bytes_up", "bytes_down"} {
					m[k] = metric(targets, "generic_http", k) + metric(targets, "generic_https", k)
				}
				if p["provider"] == "net00" {
					delete(targets, "amazon_pdp")
					gm := targets["google_search"].(map[string]any)["metrics"].(map[string]any)
					gm["bytes_up"], gm["bytes_down"] = nil, nil
				}
			}
		}
	})
	res, err := Build(in)
	if err != nil {
		t.Fatal(err)
	}
	return res.Pages
}

func metric(targets map[string]any, id, key string) float64 {
	return targets[id].(map[string]any)["metrics"].(map[string]any)[key].(float64)
}

func trafficOf(t *testing.T, ans *NetworkAnswer, rangeID string) map[string]TargetTraffic {
	t.Helper()
	out := map[string]TargetTraffic{}
	for _, tt := range ans.Ranges[rangeID].Targets {
		out[tt.Target] = tt
	}
	if len(out) != 11 || ans.Ranges[rangeID].Targets[0].Target != "generic_http" {
		t.Fatalf("%s %s: targets %v", ans.Network.Slug, rangeID, ans.Ranges[rangeID].Targets)
	}
	return out
}

// TARGET-TRAFFIC-1: generic_http + generic_https add up to the network's
// stats.<range>.bandwidthMb in every range, and each is its bytes ÷ 10⁶.
func TestTargetTrafficHTTPSumIsTheNetworksBandwidth(t *testing.T) {
	t.Parallel()
	pg := trafficPages(t)
	for _, slug := range []string{"net00", "net01", "net02"} {
		ans := pg.Network[slug+"/residential"]
		for _, r := range Ranges {
			got := trafficOf(t, ans, r.ID)
			http, https := got["generic_http"], got["generic_https"]
			total := ans.Stats[r.ID].BandwidthMb
			if !total.OK() || math.Abs(http.BandwidthMb.F()+https.BandwidthMb.F()-total.F()) > 1e-9 {
				t.Errorf("%s %s: http %v + https %v, stats %v", slug, r.ID, http.BandwidthMb, https.BandwidthMb, total)
			}
			if http.BandwidthMb != 4 || https.BandwidthMb != 0.8 || http.Requests != 5000 || https.Requests != 1000 {
				t.Errorf("%s %s: http %+v, https %+v", slug, r.ID, http, https)
			}
		}
	}
}

// A group target's requests and bandwidth are its sources' sums.
func TestTargetTrafficGroupIsItsSourcesSum(t *testing.T) {
	t.Parallel()
	ans := trafficPages(t).Network["net01/residential"]
	for _, r := range Ranges {
		got := trafficOf(t, ans, r.ID)
		group, requests, mb := got[testscale.GroupTarget], Num(0), Num(0)
		for _, s := range testscale.GroupSources() {
			requests, mb = requests+got[s].Requests, mb+got[s].BandwidthMb
		}
		if group.Requests != requests || math.Abs(group.BandwidthMb.F()-mb.F()) > 1e-9 || group.Requests <= 0 {
			t.Errorf("%s: group %+v, sources %v requests %v MB", r.ID, group, requests, mb)
		}
	}
	month := trafficOf(t, ans, "30d")
	for _, pt := range ans.Profile.Targets {
		if pt.BandwidthMb != month[pt.Target].BandwidthMb {
			t.Errorf("profile %s: %v MB, 30d %v", pt.Target, pt.BandwidthMb, month[pt.Target].BandwidthMb)
		}
	}
}

// A target without a run in the range has requests 0 and a null bandwidthMb
// with no_run_in_window; one with runs but no bytes not_measured. The
// profile's targets say the same.
func TestTargetTrafficWithoutRunOrBytes(t *testing.T) {
	t.Parallel()
	pg := trafficPages(t)
	ans := pg.Network["net00/residential"]
	for _, r := range Ranges {
		got := trafficOf(t, ans, r.ID)
		amazon, google := got["amazon_pdp"], got["google_search"]
		if amazon.Requests != 0 || amazon.BandwidthMb.OK() || google.Requests <= 0 || google.BandwidthMb.OK() {
			t.Errorf("%s: amazon %+v, google %+v", r.ID, amazon, google)
		}
		at := "ranges." + r.ID + ".targets["
		if c := ans.ReasonOf(at + "amazon_pdp].bandwidthMb"); c != NANoRun {
			t.Errorf("%s amazon_pdp: %q, want %s", r.ID, c, NANoRun)
		}
		if c := ans.ReasonOf(at + "google_search].bandwidthMb"); c != NANotMeasured {
			t.Errorf("%s google_search: %q, want %s", r.ID, c, NANotMeasured)
		}
	}
	p := ans.Profile
	if c := p.ReasonOf("targets[amazon_pdp].bandwidthMb"); c != NANoRun {
		t.Errorf("profile amazon_pdp: %q, want %s", c, NANoRun)
	}
	if c := p.ReasonOf("targets[google_search].bandwidthMb"); c != NANotMeasured {
		t.Errorf("profile google_search: %q, want %s", c, NANotMeasured)
	}
	// An unmeasured network's targets are all not_measured, never a run gap.
	u := pg.Network["net03/residential"]
	if u == nil || u.Profile == nil {
		t.Fatal("no unmeasured net03 answer")
	}
	if c := u.ReasonOf("ranges.24h.targets[amazon_pdp].bandwidthMb"); c != NANotMeasured {
		t.Errorf("unmeasured net03: %q, want %s", c, NANotMeasured)
	}
	for _, pt := range u.Profile.Targets {
		if c := u.Profile.ReasonOf("targets[" + pt.Target + "].bandwidthMb"); c != NANotMeasured {
			t.Errorf("unmeasured net03 profile %s: %q, want %s", pt.Target, c, NANotMeasured)
		}
	}
}

// A profile target decodes back with its bandwidthMb (null as NaN).
func TestProfileTargetRoundTrips(t *testing.T) {
	t.Parallel()
	for _, in := range []string{`{"target":"generic_http","requests":5,"bandwidthMb":1.5}`, `{"target":"x","requests":0,"bandwidthMb":null}`} {
		var v ProfileTarget
		if err := json.Unmarshal([]byte(in), &v); err != nil {
			t.Fatal(err)
		}
		out, err := json.Marshal(v)
		if err != nil {
			t.Fatal(err)
		}
		var back ProfileTarget
		if err := json.Unmarshal(out, &back); err != nil || back.Target != v.Target || !sameNum(back.BandwidthMb, v.BandwidthMb) {
			t.Errorf("%s: %s, back %+v (%v)", in, out, back, err)
		}
	}
	if err := (&ProfileTarget{}).UnmarshalJSON([]byte(`[`)); err == nil {
		t.Error("bad JSON decoded")
	}
}
