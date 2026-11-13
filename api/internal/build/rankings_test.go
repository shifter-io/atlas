package build

import (
	"context"
	"encoding/json"
	"slices"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/Zeus-Development/proxy-index/api/internal/store"
	"github.com/Zeus-Development/proxy-index/api/internal/testscale"
)

// shapePages builds the per-page answers of a synthetic build after edit has
// changed its published snapshot.
func shapePages(t *testing.T, shape testscale.Shape, edit func(doc map[string]any)) *Pages {
	t.Helper()
	in, err := testscale.Inputs(shape)
	if err != nil {
		t.Fatal(err)
	}
	if edit != nil {
		in.Snapshot = editSnapshot(t, in.Snapshot, edit)
	}
	res, err := Build(in)
	if err != nil {
		t.Fatal(err)
	}
	return res.Pages
}

func editSnapshot(t *testing.T, row *store.SnapshotRow, edit func(doc map[string]any)) *store.SnapshotRow {
	t.Helper()
	var doc map[string]any
	if err := json.Unmarshal(row.Payload, &doc); err != nil {
		t.Fatal(err)
	}
	edit(doc)
	payload, err := json.Marshal(doc)
	if err != nil {
		t.Fatal(err)
	}
	out := *row
	out.Payload = payload
	return &out
}

// setScore sets a network's balanced score of one window in every group.
func setScore(doc map[string]any, provider, window string, score float64) {
	for _, p := range doc["products"].([]any) {
		if p := p.(map[string]any); p["provider"] == provider {
			s := p["windows"].(map[string]any)[window].(map[string]any)["scores"].(map[string]any)["balanced"].(map[string]any)
			s["score"], s["grade"], s["rank"] = score, "B", 1
		}
	}
}

func TestRankingsListEveryNetworkInScoreOrderPerRange(t *testing.T) {
	t.Parallel()
	pg := shapePages(t, testscale.Shape{Networks: 3, Countries: 1, Exits: 3, Tail: 5, Unmeasured: 1}, func(doc map[string]any) {
		setScore(doc, "net01", "24h", 90)
		setScore(doc, "net02", "24h", 70)
		setScore(doc, "net02", "7d", 95)
		setScore(doc, "net01", "7d", 60)
		setScore(doc, "net00", "7d", 50)
	})
	slugs := func(r string) (out []string) {
		for i, n := range pg.Rankings[r].Networks {
			if n.Position != i+1 {
				t.Fatalf("%s: %s at position %d, index %d", r, n.Slug, n.Position, i)
			}
			out = append(out, n.Slug)
		}
		return out
	}
	// net00 keeps the fixture's 24h score (100); each range has its own order.
	if got := slugs("24h"); len(got) != 4 || got[0] != "net00" || got[1] != "net01" || got[2] != "net02" || got[3] != "net03" {
		t.Fatalf("24h order %v", got)
	}
	if got := slugs("7d"); got[0] != "net02" || got[1] != "net01" || got[2] != "net00" || got[3] != "net03" {
		t.Fatalf("7d order %v", got)
	}
	first := pg.Rankings["7d"].Networks[0]
	if first.Score != 95 || model.SV(first.Grade) != "B" || first.Rank != 1 {
		t.Fatalf("7d first %+v", first)
	}
	// The unmeasured network has no score and comes last, with N/A numbers.
	last := pg.Rankings["24h"].Networks[3]
	if last.Score.OK() || last.Rank.OK() || last.SuccessRate.OK() || last.Trend == nil {
		t.Fatalf("unmeasured %+v", last)
	}
	if pg.Rankings["24h"].Profile != RankingProfile || len(pg.Rankings) != 3 {
		t.Fatalf("rankings %v", pg.Rankings)
	}
	row := pg.Rankings["24h"].Networks[0]
	if !row.SuccessRate.OK() || !row.LatencyP50.OK() || len(row.Trend) == 0 || len(row.Trend) > TrendPoints {
		t.Fatalf("headline numbers %+v", row)
	}
}

func TestTheAllNetworksAnswerCountsDistinctIpsNeverASum(t *testing.T) {
	t.Parallel()
	in, err := testscale.NetworkInputs(testscale.NetworkShape{Shape: testscale.Shape{Networks: 2, Countries: 1, Exits: 3, Tail: 60}, Requested: 2})
	if err != nil {
		t.Fatal(err)
	}
	res, err := Build(in)
	if err != nil {
		t.Fatal(err)
	}
	pg := res.Pages
	ans := pg.Network[AllNetworks+"/"+AllNetworks]
	if ans == nil || pg.Primary[AllNetworks] != AllNetworks || ans.Networks != 2 || ans.ProductGroups != 4 || ans.Network.Slug != AllNetworks {
		t.Fatalf("answer %+v", ans)
	}
	one := pg.Network["net00/residential"]
	for _, r := range Ranges {
		all, net := ans.Ranges[r.ID], one.Ranges[r.ID]
		// Every request counts once: the two networks' summaries add up.
		if all.Requests != 2*net.Requests || all.UniqueIps != testscale.FleetUniqueIps || all.IpsSum.OK() || !all.LatencyP50.OK() ||
			!all.LatencyP99.OK() || all.SuccessRate != ratio(all.Successes, all.Requests) {
			t.Fatalf("%s totals %+v", r.ID, all)
		}
		// A network's summary has its own distinct IPs and latency.
		if !net.UniqueIps.OK() || !net.LatencyP50.OK() {
			t.Fatalf("%s network totals %+v", r.ID, net)
		}
	}
	month := ans.Ranges["30d"]
	// Places are a union: both networks saw AA-AC, and cities 0-59 and 1-60.
	if month.Lists["city"] != 61 || month.Lists["country"] != 3 || month.ExitCountries != 3 {
		t.Fatalf("lists %v", month.Lists)
	}
	if ans.Network.UniqueIps != testscale.FleetUniqueIps || ans.Network.Countries != 3 || ans.Network.SuccessRate != month.SuccessRate {
		t.Fatalf("head %+v", ans.Network)
	}
	// All of them have no name of their own (UNREAD-TEXT-1, #977); a network has its own.
	if ans.Network.Name != nil || !slices.Contains(ans.NA, "network.name") || ans.ReasonOf("network.name") != NANotApplicable ||
		model.SV(one.Network.Name) == "" {
		t.Errorf("all-networks name %v, na %v %s %v; net00 %v", ans.Network.Name, ans.NA, ans.NAList.Reason, ans.NAList.Reasons, one.Network.Name)
	}
	for i, d := range ans.Days {
		if d.UniqueIps != Num(testscale.FleetBucketIps+i) || d.IpsSum.OK() {
			t.Fatalf("day %d %+v", i, d)
		}
	}
	page, ok, _ := pg.List(context.Background(), "", AllNetworks+"/"+AllNetworks, "30d", "city", "", 2)
	if !ok || page.Total != 61 || len(page.Rows) != 11 {
		t.Fatalf("all-networks city page %+v", page)
	}
}

func TestWithoutAnEveryGroupFleetTheDistinctFiguresAreNA(t *testing.T) {
	t.Parallel()
	pg := shapePages(t, testscale.Shape{Networks: 2, Countries: 1, Exits: 3, Tail: 5, NoFleetAll: true}, nil)
	ans := pg.Network[AllNetworks+"/"+AllNetworks]
	for _, r := range Ranges {
		if t0 := ans.Ranges[r.ID]; t0.UniqueIps.OK() || t0.LatencyP50.OK() || t0.IpsSum.OK() || !t0.Requests.OK() {
			t.Fatalf("%s %+v", r.ID, t0)
		}
	}
	if ans.Network.UniqueIps.OK() || ans.Days[0].UniqueIps.OK() {
		t.Fatalf("head %+v day %+v", ans.Network, ans.Days[0])
	}
	// One group per network: its own distinct IPs and latency are the network's.
	single := pg.Network["net00/residential"]
	net, dayIps := single.Ranges["24h"], 0
	for _, d := range single.Days {
		if d.UniqueIps.OK() && d.UniqueIps == d.IpsSum {
			dayIps++
		}
	}
	if !net.UniqueIps.OK() || net.UniqueIps != net.IpsSum || !net.LatencyP50.OK() || dayIps == 0 {
		t.Fatalf("single-group network %+v, %d days with distinct IPs", net, dayIps)
	}
}

func TestANetworkNamedAllHidesTheAggregate(t *testing.T) {
	t.Parallel()
	pg := shapePages(t, testscale.Shape{Networks: 1, Countries: 1, Exits: 3, Tail: 5}, nil)
	if pg.Network["all/all"] == nil {
		t.Fatal("no aggregate")
	}
	e := &entry{P: &model.Provider{Slug: AllNetworks, Name: "All", PrimaryKind: "residential", ProductMeasurements: model.NewMeasurementMap()}}
	stats := map[string]*WindowStats{}
	for _, r := range Ranges {
		stats[r.ID] = &WindowStats{}
	}
	pg, err := buildPages(context.Background(), "", []*entry{e}, map[string]map[string]*WindowStats{AllNetworks: stats}, nil, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	if pg.Network["all/all"] != nil || pg.Network["all/residential"] == nil {
		t.Fatalf("networks %v", pg.Network)
	}
}
