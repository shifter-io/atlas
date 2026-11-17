package build

import (
	"context"
	"encoding/json"
	"testing"
	"time"

	"github.com/Zeus-Development/atlas/api/internal/snap"
	"github.com/Zeus-Development/atlas/api/internal/store"
	"github.com/Zeus-Development/atlas/api/internal/testscale"
)

// keptBuild is smallNetBuild with net01's 7d network window holding a list
// its build kept from 3 hours before the window's end (CALC-SCALE-4), and
// that earlier time.
func keptBuild(t *testing.T) (*Result, string) {
	t.Helper()
	in, err := testscale.Inputs(testscale.Shape{Networks: 3, Countries: 1, Exits: 3, Tail: 60, CityAsn: 120})
	if err != nil {
		t.Fatal(err)
	}
	head, lists, err := testscale.Networks(in.Snapshot, 3)
	if err != nil {
		t.Fatal(err)
	}
	var doc map[string]any
	if err = json.Unmarshal(head, &doc); err != nil {
		t.Fatal(err)
	}
	kept := ""
	for _, n := range doc["networks"].([]any) {
		e := n.(map[string]any)
		if e["provider"] != "net01" {
			continue
		}
		w := e["windows"].(map[string]any)["7d"].(map[string]any)
		end, ok := snap.ParseTime(w["period"].(map[string]any)["end"].(string))
		if !ok {
			t.Fatalf("7d end %v", w["period"])
		}
		kept = snap.JSISO(end.Add(-3 * time.Hour))
		w["breakdowns_as_of"] = end.Add(-3 * time.Hour).Format(time.RFC3339)
	}
	if kept == "" {
		t.Fatal("no net01 network")
	}
	if head, err = json.Marshal(doc); err != nil {
		t.Fatal(err)
	}
	in.Snapshot = &store.SnapshotRow{BuildID: in.Snapshot.BuildID, ContentHash: in.Snapshot.ContentHash, Payload: head}
	if err = in.Snapshot.SetNetworks(lists); err != nil {
		t.Fatal(err)
	}
	res, err := Build(in)
	if err != nil {
		t.Fatal(err)
	}
	return res, kept
}

// A window's place rows are counted to its end, but a kept list's to its
// cutoff: the network's range says so, and every answer merging it says the
// oldest.
func TestBreakdownAsOfIsTheKeptListsCutoff(t *testing.T) {
	t.Parallel()
	res, kept := keptBuild(t)
	pg, ctx := res.Pages, context.Background()
	own, other := pg.Network["net01/residential"], pg.Network["net00/residential"]
	if own == nil || other == nil {
		t.Fatal("no net00 or net01 answer")
	}
	end := other.Ranges["7d"].BreakdownAsOf
	if own.Ranges["7d"].BreakdownAsOf != kept || end == "" || end <= kept {
		t.Fatalf("net01 7d %q, net00 7d %q, kept %q", own.Ranges["7d"].BreakdownAsOf, end, kept)
	}
	if own.Ranges["30d"].BreakdownAsOf != other.Ranges["30d"].BreakdownAsOf || own.Ranges["24h"].BreakdownAsOf == "" {
		t.Errorf("net01 30d %q (net00 %q), 24h %q", own.Ranges["30d"].BreakdownAsOf, other.Ranges["30d"].BreakdownAsOf,
			own.Ranges["24h"].BreakdownAsOf)
	}
	all := AllNetworks + "/" + AllNetworks
	if pg.Network[all].Ranges["7d"].BreakdownAsOf != kept || pg.asOf["7d"] != kept {
		t.Errorf("all networks 7d %q, every network %q", pg.Network[all].Ranges["7d"].BreakdownAsOf, pg.asOf["7d"])
	}
	for scope, want := range map[string]string{"net01/residential": kept, "net00/residential": end, all: kept} {
		page, ok, err := pg.List(ctx, "", scope, "7d", "city", ListFilter{}, 1)
		if !ok || err != nil || page.BreakdownAsOf != want {
			t.Errorf("%s cities: %v %v, breakdownAsOf %q, want %q", scope, ok, err, page.BreakdownAsOf, want)
		}
		if scope == all {
			continue
		}
		for _, p := range []int{1, 2} {
			c, ok, err := pg.Coverage(ctx, "", scope, "7d", ListFilter{}, p)
			if !ok || err != nil || c.BreakdownAsOf != want {
				t.Errorf("%s coverage page %d: %v %v, breakdownAsOf %q, want %q", scope, p, ok, err, c.BreakdownAsOf, want)
			}
		}
	}
	for cc, ans := range pg.Country {
		if ans.BreakdownAsOf["7d"] != kept || ans.BreakdownAsOf["30d"] != own.Ranges["30d"].BreakdownAsOf {
			t.Errorf("country %s: %v", cc, ans.BreakdownAsOf)
		}
	}
	for key := range pg.placeKeys[placeList{"7d", "city"}] {
		ans, ok, err := pg.Place(ctx, "", "city", key, "7d")
		if !ok || err != nil || ans.BreakdownAsOf != kept {
			t.Errorf("city %s: %v %v, breakdownAsOf %q", key, ok, err, ans.BreakdownAsOf)
		}
		break
	}
}
