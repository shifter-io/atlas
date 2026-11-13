package build

import (
	"encoding/json"
	"slices"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/store"
	"github.com/Zeus-Development/proxy-index/api/internal/testscale"
)

// noRunNetwork is the network whose 24h window has no run in noRunInputs.
const noRunNetwork = "net05"

// noRunInputs is the week's inputs with noRunNetwork tested on its first day
// only (#815's byteful): its 24h window has no completed request, no latency
// and no IP, but the hour formula's figures for 24h and 7d (5,000 and 7,000
// IPs, breakdown scale 2), which a window with a run serves.
func noRunInputs(t *testing.T) *store.Inputs {
	t.Helper()
	in, err := testscale.WeekInputs(testscale.Shape{Networks: 18, Countries: 1, Exits: 3, Tail: 30, Browser: true})
	if err != nil {
		t.Fatal(err)
	}
	var doc map[string]any
	if err := json.Unmarshal(in.Snapshot.Payload, &doc); err != nil {
		t.Fatal(err)
	}
	for _, p := range doc["products"].([]any) {
		p := p.(map[string]any)
		if p["provider"] != noRunNetwork {
			continue
		}
		windows := p["windows"].(map[string]any)
		day := windows["24h"].(map[string]any)
		m := day["metrics"].(map[string]any)
		m["completed"], m["successes"], m["success_rate"], m["latency_ms"] = 0, 0, nil, map[string]any{}
		day["pool"].(map[string]any)["unique_ips"], day["series"] = 0, []any{}
		day["overlap"] = map[string]any{"exclusive_share": nil, "shared_share": nil, "compared_products": 0, "peers": []any{}}
		day["estimated"] = map[string]any{"unique_ips": map[string]any{"value": 5000}, "breakdown_scale": 2,
			"series": []any{map[string]any{"start": "2026-11-02T12:00:00+00:00", "unique_ips": 300}}}
		windows["7d"].(map[string]any)["estimated"] = map[string]any{"unique_ips": map[string]any{"value": 7000}, "breakdown_scale": 2}
	}
	if in.Snapshot.Payload, err = json.Marshal(doc); err != nil {
		t.Fatal(err)
	}
	return in
}

// A window with no run of the network has null IP figures with
// no_run_in_window in every answer, though the formula has an estimate for
// it; its other nulls take the same reason; the 7d window, which has a run,
// keeps the estimate (NO-RUN-WINDOW-1).
func TestAWindowWithoutARunHasNoEstimateAndNoRunReasons(t *testing.T) {
	t.Parallel()
	res, err := Build(noRunInputs(t))
	if err != nil {
		t.Fatal(err)
	}
	pg := res.Pages
	reasons := []struct {
		of    *NAList
		paths []string
	}{
		{&rankingRowOf(t, pg.Rankings["24h"], noRunNetwork).NAList, []string{"uniqueIps", "successRate", "downtimeMinutes"}},
		{&homeRowOf(t, pg.Registered["/v1/home/24h"].(*HomeAnswer), noRunNetwork).NAList, []string{"uniqueIps", "successRate"}},
		{&pg.Network[noRunNetwork+"/residential"].NAList, []string{"ranges.24h.uniqueIps", "ranges.24h.ipsSum", "ranges.24h.successRate",
			"ranges.24h.latencyP50", "ranges.24h.latencyP90", "ranges.24h.latencyP99"}},
		{&pg.Network[noRunNetwork+"/residential"].Stats["24h"].NAList, []string{"uniqueIps", "successRate", "downtimeMinutes"}},
		{&pg.Network[noRunNetwork+"/residential"].Profile.NAList, []string{"pool.uniqueIps24h"}},
		{&overlapRowOf(t, pg.Registered["/v1/overlap/residential/24h"].(*OverlapAnswer), noRunNetwork).NAList,
			[]string{"exclusiveShare", "sharedShare", "label"}},
	}
	for i, r := range reasons {
		for _, p := range r.paths {
			if got := r.of.ReasonOf(p); got != NANoRun {
				t.Errorf("%d %s: %q, want %s (na %v)", i, p, got, NANoRun, r.of.NA)
			}
		}
	}
	if pts := pg.Charts[noRunNetwork+"/residential/24h"].Pool; len(pts) != 0 {
		t.Errorf("24h pool chart %v", pts)
	}
	if ips := pg.Network[noRunNetwork+"/residential"].Ranges["24h"].UniqueIps; ips.OK() {
		t.Errorf("24h unique IPs %v", ips)
	}
	for _, p := range res.Dataset.Providers {
		if p.Slug == noRunNetwork && (p.Pool.UniqueIps24h.OK() || !slices.Contains(p.NA, "pool.uniqueIps24h")) {
			t.Errorf("dataset uniqueIps24h %v, na %v", p.Pool.UniqueIps24h, p.NA)
		}
	}
	// The 7d window has a run: its estimate stands, and its rows keep a value.
	if ips := rankingRowOf(t, pg.Rankings["7d"], noRunNetwork).UniqueIps; ips != 7000 {
		t.Errorf("7d unique IPs %v, want the estimate", ips)
	}
	if ips := pg.Network[noRunNetwork+"/residential"].Ranges["7d"].UniqueIps; ips != 7000 {
		t.Errorf("7d network unique IPs %v", ips)
	}
	// Another network's 24h window has a run: no no_run_in_window, and its
	// overlap row's nulls are the calculator's.
	other := rankingRowOf(t, pg.Rankings["24h"], "net04")
	if !other.UniqueIps.OK() || other.ReasonOf("uniqueIps") != "" || other.NAList.Reason == NANoRun {
		t.Errorf("net04 24h: %v, na %v %s", other.UniqueIps, other.NA, other.NAList.Reason)
	}
	for _, rangeID := range []string{"24h", "7d"} {
		row := overlapRowOf(t, pg.Registered["/v1/overlap/residential/"+rangeID].(*OverlapAnswer), "net04")
		for _, p := range row.NA {
			if row.ReasonOf(p) != NAInsufficient {
				t.Errorf("net04 overlap %s %s: %s", rangeID, p, row.ReasonOf(p))
			}
		}
	}
}

func rankingRowOf(t *testing.T, a *RankingsAnswer, slug string) *RankingRow {
	t.Helper()
	for i := range a.Networks {
		if a.Networks[i].Slug == slug {
			return &a.Networks[i]
		}
	}
	t.Fatalf("no ranking row %s", slug)
	return nil
}

func homeRowOf(t *testing.T, a *HomeAnswer, slug string) *HomeNetwork {
	t.Helper()
	for i := range a.Networks {
		if a.Networks[i].Slug == slug {
			return &a.Networks[i]
		}
	}
	t.Fatalf("no home row %s", slug)
	return nil
}

func overlapRowOf(t *testing.T, a *OverlapAnswer, slug string) *OverlapNetwork {
	t.Helper()
	for i := range a.Networks {
		if a.Networks[i].Slug == slug {
			return &a.Networks[i]
		}
	}
	t.Fatalf("no overlap row %s", slug)
	return nil
}
