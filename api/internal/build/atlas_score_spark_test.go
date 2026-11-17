package build

import (
	"bytes"
	"encoding/json"
	"slices"
	"testing"

	"github.com/Zeus-Development/atlas/api/internal/snap"
	"github.com/Zeus-Development/atlas/api/internal/testscale"
)

// atlasSparkProduct is a product whose 24h window carries three Atlas Score
// points (ATLAS-SCORE-SPARK-1): an hour without a build, a build that
// published no score and the build's own score.
func atlasSparkProduct(t *testing.T) *snap.Product {
	t.Helper()
	return decodeProduct(t, `{"id":"p","windows":{"24h":{"atlas_score_spark":[
 {"at":"2026-11-16T13:10:00+00:00","score":null,"stored":false},
 {"at":"2026-11-16T14:10:00+00:00","score":null,"stored":true},
 {"at":"2026-11-16T15:10:00+00:00","score":76.35979768422267,"stored":true}]}}}`)
}

func statusesOf(s ScoreSeries) []string {
	out := []string{}
	for _, st := range s.Statuses {
		if st == nil {
			out = append(out, "<nil>")
			continue
		}
		out = append(out, *st)
	}
	return out
}

// Each point is the calculator's score at 1 decimal, null with its reason
// (no_history, not_scored) otherwise, at its minutes before the build.
func TestTheAtlasScoreSparkIsTheCalculatorsPoints(t *testing.T) {
	t.Parallel()
	s := productAtlasSpark(atlasSparkProduct(t), "24h")
	if len(s.Values) != 3 || s.Values[0].OK() || s.Values[1].OK() || s.Values[2] != 76.4 {
		t.Errorf("values %v, want null, null, 76.4", s.Values)
	}
	if got := statusesOf(s.ScoreSeries); !slices.Equal(got, []string{atlasNoHistory, atlasNotScored, scoreRanked}) {
		t.Errorf("statuses %v", got)
	}
	if !slices.Equal(s.Times, []Num{120, 60, 0}) {
		t.Errorf("times %v, want 120 60 0", s.Times)
	}
	// A window without one (a snapshot from before it) and no product: empty lists.
	for name, e := range map[string]AtlasScoreSeries{"7d": productAtlasSpark(atlasSparkProduct(t), "7d"), "none": productAtlasSpark(nil, "24h")} {
		body, err := json.Marshal(e)
		if err != nil || string(body) != `{"values":[],"statuses":[],"times":[]}` {
			t.Errorf("%s: %s %v", name, body, err)
		}
	}
}

// A home network's spark is its ranking row's, its times kept once in the
// answer's times; the network page's ranges carry their window's.
func TestTheAtlasSparkReachesTheHomeAndTheNetworkPage(t *testing.T) {
	t.Parallel()
	s := productAtlasSpark(atlasSparkProduct(t), "24h")
	row := RankingRow{AtlasScoreSpark: s.ScoreSeries, AtlasScoreSparkT: s.Times}
	a := &HomeAnswer{Times: [][]Num{{5}}}
	n, m := a.atlasSpark(row), a.atlasSpark(row)
	if n.TimesIndex != 1 || m.TimesIndex != 1 || len(a.Times) != 2 || !slices.Equal(a.Times[1], s.Times) {
		t.Errorf("times index %d %d of %v", n.TimesIndex, m.TimesIndex, a.Times)
	}
	if !slices.Equal(statusesOf(n.ScoreSeries), statusesOf(s.ScoreSeries)) || len(n.Values) != 3 {
		t.Errorf("home spark %+v", n)
	}
	page := &NetworkAnswer{Ranges: map[string]*RangeTotals{}}
	for _, r := range Ranges {
		page.Ranges[r.ID] = &RangeTotals{}
	}
	page.setRangeScores(atlasSparkProduct(t))
	if got := page.Ranges["24h"].AtlasScoreSpark; got == nil || !slices.Equal(got.Times, s.Times) || got.Values[2] != 76.4 {
		t.Errorf("network page 24h spark %+v", got)
	}
	if got := page.Ranges["30d"].AtlasScoreSpark; got == nil || len(got.Values) != 0 {
		t.Errorf("network page 30d spark %+v", got)
	}
}

// Every ranking row and home network of the fixture serves the spark, one
// status and one time per value (empty: the fixture snapshots predate it).
func TestEveryRowServesAnAtlasSpark(t *testing.T) {
	t.Parallel()
	pg, _ := homePages(t)
	for _, r := range Ranges {
		for _, row := range pg.Rankings[r.ID].Networks {
			s := row.AtlasScoreSpark
			if s.Values == nil || len(s.Statuses) != len(s.Values) || len(row.AtlasScoreSparkT) != len(s.Values) {
				t.Errorf("%s %s: ranking spark %+v times %v", r.ID, row.Slug, s, row.AtlasScoreSparkT)
			}
		}
		h := homeOf(t, pg, r.ID)
		for _, n := range h.Networks {
			s := n.AtlasScoreSpark
			if len(s.Statuses) != len(s.Values) || s.TimesIndex >= len(h.Times) || len(h.Times[s.TimesIndex]) != len(s.Values) {
				t.Errorf("%s %s: home spark %+v", r.ID, n.Slug, s)
			}
		}
		body, err := json.Marshal(h)
		if err != nil || len(h.Networks) > 0 && !bytes.Contains(body, []byte(`"atlasScoreSpark":{"values":`)) {
			t.Errorf("%s: home answer without atlasScoreSpark (%v)", r.ID, err)
		}
	}
}

// A snapshot whose net00 24h windows carry the calculator's points serves
// them on its ranking row, its home row and its network page's 24h range.
func TestASnapshotsAtlasSparkIsServedEverywhere(t *testing.T) {
	t.Parallel()
	in, err := testscale.Inputs(testscale.Shape{Networks: 3, Countries: 1, Exits: 3, Tail: 5})
	if err != nil {
		t.Fatal(err)
	}
	points := []any{map[string]any{"at": "2026-11-16T14:10:00+00:00", "score": 70.04, "stored": true},
		map[string]any{"at": "2026-11-16T15:10:00+00:00", "score": 71.06, "stored": true}}
	in.Snapshot = editSnapshot(t, in.Snapshot, func(doc map[string]any) {
		for _, p := range doc["products"].([]any) {
			if p := p.(map[string]any); p["provider"] == "net00" {
				p["windows"].(map[string]any)["24h"].(map[string]any)["atlas_score_spark"] = points
			}
		}
	})
	res, err := Build(in)
	if err != nil {
		t.Fatal(err)
	}
	pg := res.Pages
	want := []Num{70, 71.1}
	for _, row := range pg.Rankings["24h"].Networks {
		if row.Slug == "net00" && (!slices.Equal(row.AtlasScoreSpark.Values, want) || !slices.Equal(row.AtlasScoreSparkT, []Num{60, 0})) {
			t.Errorf("ranking row %+v %v", row.AtlasScoreSpark, row.AtlasScoreSparkT)
		}
	}
	h := homeOf(t, pg, "24h")
	for _, n := range h.Networks {
		if n.Slug == "net00" && (!slices.Equal(n.AtlasScoreSpark.Values, want) || !slices.Equal(h.Times[n.AtlasScoreSpark.TimesIndex], []Num{60, 0})) {
			t.Errorf("home row %+v", n.AtlasScoreSpark)
		}
	}
	if s := pg.Network["net00/residential"].Ranges["24h"].AtlasScoreSpark; s == nil || !slices.Equal(s.Values, want) {
		t.Errorf("network page %+v", s)
	}
	if s := pg.Network[AllNetworks+"/"+AllNetworks].Ranges["24h"].AtlasScoreSpark; s != nil {
		t.Errorf("all-networks page %+v, want null", s)
	}
}
