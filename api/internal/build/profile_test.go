package build

import (
	"encoding/json"
	"fmt"
	"strings"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/Zeus-Development/proxy-index/api/internal/store"
	"github.com/Zeus-Development/proxy-index/api/internal/testscale"
)

func TestANetworkAnswerCarriesItsProfileWithoutTheLongLists(t *testing.T) {
	t.Parallel()
	in, err := testscale.Inputs(testscale.Shape{Networks: 2, Countries: 2, Exits: 3, Tail: 60})
	if err != nil {
		t.Fatal(err)
	}
	res, err := Build(in)
	if err != nil {
		t.Fatal(err)
	}
	pg, d := res.Pages, res.Details["net00"]
	ans := pg.Network["net00/residential"]
	if ans.Profile == nil || ans.Profile.Slug != "net00" || ans.Profile.Pool.UniqueIps30d != d.Provider.Pool.UniqueIps30d ||
		ans.Profile.TileRanks == nil || ans.Reviews == nil || ans.Tape == nil || len(ans.Stats) != 3 {
		t.Fatalf("answer %+v", ans)
	}
	body, err := json.Marshal(ans)
	if err != nil {
		t.Fatal(err)
	}
	// The raw snapshot product, the geo and ASN lists, the place ranks and the
	// histories are not repeated here.
	for _, key := range []string{`"measurement"`, `"geo":[`, `"asns":[`, `"placeRanks"`, `"countryLatencyRanks"`, `"success":`, `"availability"`, `"bands"`} {
		if strings.Contains(string(body), key) {
			t.Errorf("network answer has %s", key)
		}
	}
	for _, r := range Ranges {
		ws := d.Windows["residential"][r.ID]
		chart := pg.Charts["net00/residential/"+r.ID]
		if ans.Stats[r.ID].SuccessRate != ws.SuccessRate || ans.Stats[r.ID].LatencyP50 != ws.LatencyP50 || chart == nil ||
			len(chart.Success) == 0 || len(chart.Success) != len(ws.Success) || len(chart.Latency) != len(ws.Latency) || chart.Range != r.ID {
			t.Fatalf("%s: stats %+v chart %+v", r.ID, ans.Stats[r.ID], chart)
		}
	}
	if ans.Stats["24h"].Success != nil || ans.Stats["24h"].WindowStats.Success == nil {
		t.Fatal("the stats' histories are hidden, not dropped from the details")
	}
}

func TestTheAllNetworksChartIsTheEveryGroupFleetSeries(t *testing.T) {
	t.Parallel()
	pg := shapePages(t, testscale.Shape{Networks: 2, Countries: 2, Exits: 3, Tail: 5}, nil)
	for _, r := range Ranges {
		chart := pg.Charts["all/all/"+r.ID]
		if chart == nil || len(chart.Success) == 0 || len(chart.Success) != len(chart.Latency) || len(chart.Pool) != len(chart.Success) ||
			len(chart.Availability) != 0 || chart.Bands == nil {
			t.Fatalf("%s chart %+v", r.ID, chart)
		}
		// Distinct IPs per bucket, never a sum: the fleet entry's own values
		// (FleetBucketIps plus the bucket's index).
		for i, p := range chart.Pool {
			if p.V < testscale.FleetBucketIps || p.V >= testscale.FleetBucketIps+300 || (i > 0 && p.V <= chart.Pool[i-1].V) {
				t.Fatalf("%s pool %+v", r.ID, chart.Pool)
			}
		}
	}
	empty := shapePages(t, testscale.Shape{Networks: 1, Countries: 1, Exits: 3, Tail: 5, NoFleetAll: true}, nil)
	if c := empty.Charts["all/all/24h"]; c == nil || len(c.Success) != 0 || c.Pool == nil {
		t.Fatalf("chart without a fleet entry %+v", c)
	}
}

// A network with a long history: 400 records of 2 KB in each of three lists and
// 400 tape rows of 1 KB (twice a live row) would break the cap (2.8 MB); the
// answer keeps the newest RecordCap, the tape each tab's newest
// store.TapeHeadRows (40 rows), and the full counts.
func TestANetworkAnswerKeepsItsNewestRecordsWithinTheCap(t *testing.T) {
	t.Parallel()
	in, err := testscale.Inputs(testscale.Shape{Networks: 1, Countries: 40, Exits: 240, Tail: 200})
	if err != nil {
		t.Fatal(err)
	}
	res, err := Build(in)
	if err != nil {
		t.Fatal(err)
	}
	d := res.Details["net00"]
	body := strings.Repeat("x", 2048)
	var changes []ChangeEntry
	for i := range 400 {
		d.Reviews = append(d.Reviews, model.UserReview{ID: fmt.Sprintf("r%03d", i), ProviderSlug: "net00", Product: "residential_rotating", Body: body})
		changes = append(changes, ChangeEntry{ID: fmt.Sprintf("c%03d", i), Network: &ChangeNetwork{Slug: "net00"}, Detail: tpl(body, nil)})
		d.CommunityRuns = append(d.CommunityRuns, model.CommunityRun{ID: fmt.Sprintf("u%03d", i), Product: "residential_sticky"})
		d.Tape = append(d.Tape, model.TapeEvent{ID: fmt.Sprintf("t%03d", i), Kind: store.TapeTabs[i%4], Product: "residential", Detail: body[:1024]})
	}
	res.Pages.addDetails("", res.Details)
	res.Pages.addNetworkChangelog(changes)
	ans := res.Pages.Network["net00/residential"]
	if len(ans.Reviews) != RecordCap || ans.Reviews[0].ID != "r000" || len(ans.Changelog) != RecordCap || len(ans.CommunityRuns) != RecordCap ||
		len(ans.Tape) != 4*store.TapeHeadRows || ans.Records != (RecordCounts{Reviews: 400, CommunityRuns: 400, Changelog: 400, Tape: 400}) {
		t.Fatalf("records %d %d %d %d %+v", len(ans.Reviews), len(ans.Changelog), len(ans.CommunityRuns), len(ans.Tape), ans.Records)
	}
	out, err := json.Marshal(ans)
	if err != nil {
		t.Fatal(err)
	}
	if len(out) > MaxAnswerBytes {
		t.Fatalf("network answer %d bytes, above the cap", len(out))
	}
}
