package build

import (
	"fmt"
	"slices"
	"testing"

	"github.com/shifter-io/atlas/api/internal/model"
	"github.com/shifter-io/atlas/api/internal/snap"
)

// exclusiveRows are overlap rows with these exclusiveIps (NaN: unknown).
func exclusiveRows(ips ...float64) []OverlapNetwork {
	out := []OverlapNetwork{}
	for i, v := range ips {
		out = append(out, OverlapNetwork{Slug: fmt.Sprint("n", i), ExclusiveIps: Num(v)})
	}
	return out
}

func scoreOf(n OverlapNetwork) string {
	if s := n.ExclusiveScore; s != nil {
		return fmt.Sprintf("%.3g/%v/%v/%s", s.Score.F(), s.Rank.F(), s.RankedOf.F(), s.Status)
	}
	return "null"
}

// The score is the ratio to the leader on 1-10, never below 1; a tie shares
// the average place; a network without exclusiveIps has none and is not
// counted in rankedOf.
func TestExclusiveScoreRatioToLeaderAndTies(t *testing.T) {
	t.Parallel()
	rows := exclusiveRows(800, 400, 400, 0, nan.F(), 50)
	exclusiveScores(rows)
	got := slugsOf(rows, scoreOf)
	want := []string{"10/1/5/ranked", "5/2.5/5/ranked", "5/2.5/5/ranked", "1/5/5/ranked", "null", "1/4/5/ranked"}
	if !slices.Equal(got, want) {
		t.Errorf("scores %v, want %v", got, want)
	}
}

// No network of the kind with exclusive IPs: every one reads 1 and they
// share the place. No network with exclusiveIps: every score is null.
func TestExclusiveScoreWithoutALeader(t *testing.T) {
	t.Parallel()
	zero := exclusiveRows(0, 0)
	exclusiveScores(zero)
	if got := slugsOf(zero, scoreOf); !slices.Equal(got, []string{"1/1.5/2/ranked", "1/1.5/2/ranked"}) {
		t.Errorf("all zero %v", got)
	}
	none := exclusiveRows(nan.F(), nan.F())
	exclusiveScores(none)
	if got := slugsOf(none, scoreOf); !slices.Equal(got, []string{"null", "null"}) {
		t.Errorf("no value %v", got)
	}
}

// In ovContext's 30d, big leads (10 000 × 0.93); solo, with no peers, is
// scored on its whole pool; unknown has no share, so no score, and its null
// has a reason, its exclusiveIps'.
func TestOverlapServesTheExclusiveScore(t *testing.T) {
	t.Parallel()
	a := OverlapFor(ovContext(), "residential", "30d")
	n := byNetwork(a)
	got := []string{scoreOf(n["big"]), scoreOf(n["solo"]), scoreOf(n["mixed"]), scoreOf(n["resold"]), scoreOf(n["unknown"])}
	want := []string{"10/1/4/ranked", "5.38/2/4/ranked", "1.08/3/4/ranked", "1/4/4/ranked", "null"}
	if !slices.Equal(got, want) {
		t.Errorf("scores %v, want %v", got, want)
	}
	if err := a.listNA(); err != nil {
		t.Fatal(err)
	}
	// unknown has no run in the window: its nulls' reason is no_run_in_window.
	u := byNetwork(a)["unknown"]
	if !slices.Contains(u.NA, "exclusiveScore") || !slices.Contains(u.NA, "exclusiveIps") || u.Reason != NANoRun ||
		u.Reasons["exclusiveScore"] != "" {
		t.Errorf("unknown na %v %q %v", u.NA, u.Reason, u.Reasons)
	}
}

// A network alone in its kind is its own leader: 10, first of 1.
func TestExclusiveScoreOfANetworkWithNoPeers(t *testing.T) {
	t.Parallel()
	c := &Context{Entries: []*entry{ovEntry("solo", 1, map[string]snap.Window{"30d": ovWindow(1, 0, 5000)})},
		Snapshot: &snap.Snapshot{}}
	solo := OverlapFor(c, "residential", "30d").Networks[0]
	if solo.ExclusiveIps != 5000 || scoreOf(solo) != "10/1/1/ranked" {
		t.Errorf("solo %v %s", solo.ExclusiveIps, scoreOf(solo))
	}
}

// The headline windows and each kind's get the range's exclusiveIps and
// score; a window of another kind or without a row keeps null.
func TestAddExclusiveFigures(t *testing.T) {
	t.Parallel()
	c := ovContext()
	stats := map[string]map[string]*WindowStats{}
	details := map[string]*ProviderDetail{}
	for _, e := range c.Entries {
		e.P.PrimaryKind = "residential"
		stats[e.P.Slug], details[e.P.Slug] = map[string]*WindowStats{}, &ProviderDetail{Windows: map[string]map[string]*WindowStats{}}
		details[e.P.Slug].Windows["residential"] = map[string]*WindowStats{}
		for _, r := range Ranges {
			stats[e.P.Slug][r.ID] = &WindowStats{WindowFigures: noFigures()}
			details[e.P.Slug].Windows["residential"][r.ID] = &WindowStats{WindowFigures: noFigures()}
		}
	}
	addExclusiveFigures(c, stats, details)
	big, mixed := stats["big"]["30d"], details["mixed"].Windows["residential"]["24h"]
	if big.ExclusiveIps != 9300 || big.ExclusiveScore == nil || big.ExclusiveScore.Score != 10 {
		t.Errorf("big 30d %v %+v", big.ExclusiveIps, big.ExclusiveScore)
	}
	// 24h: mixed 800 × 0.9 = 720 against big's 4000 × 0.98 = 3920.
	if mixed.ExclusiveIps != 720 || mixed.ExclusiveScore == nil || mixed.ExclusiveScore.Rank != 3 {
		t.Errorf("mixed 24h %v %+v", mixed.ExclusiveIps, mixed.ExclusiveScore)
	}
	if u := stats["unknown"]["30d"]; u.ExclusiveIps.OK() || u.ExclusiveScore != nil {
		t.Errorf("unknown %+v", u.WindowFigures)
	}
	if co := stats["catalog-only"]["30d"]; co.ExclusiveIps.OK() || co.ExclusiveScore != nil {
		t.Errorf("catalog-only %+v", co.WindowFigures)
	}
}

// insights.exclusivity ranks by exclusiveScore, not the share: big (9 300
// IPs, 93%) leads solo (5 000, 100%); equal scores go by name, and a
// network without a score (dc: no overlap row; unknown) comes last.
func TestHomeExclusivityRanksByScore(t *testing.T) {
	t.Parallel()
	c := insightContext()
	ex := homeInsights(c, insightRows(), "30d").Exclusivity
	if !sameSlugs(slugsOf(ex, func(r HomeExclusiveRow) string { return r.Slug }), "big", "solo", "mixed", "resold", "dc") {
		t.Fatalf("exclusivity %+v", ex)
	}
	if ex[0].ExclusiveIps != 9300 || ex[0].PoolIps != 10000 || ex[0].Value != 0.93 || ex[1].Value != 1 ||
		ex[4].ExclusiveScore != nil || ex[4].ExclusiveIps.OK() || ex[4].PoolIps.OK() {
		t.Errorf("rows %+v", ex)
	}
	// A second kind's only network also reads 10: the tie goes by name.
	c.Entries = append(c.Entries, &entry{P: &model.Provider{Slug: "dc", Name: "Net dc", PrimaryKind: "datacenter"},
		Snap: map[string]*snap.Product{"datacenter": {ProductID: 9, Cohort: "network",
			Windows: map[string]snap.Window{"30d": ovWindow(1, 0, 10)}}}})
	ex = homeInsights(c, insightRows(), "30d").Exclusivity
	if !sameSlugs(slugsOf(ex, func(r HomeExclusiveRow) string { return r.Slug }), "big", "dc", "solo", "mixed", "resold") {
		t.Errorf("tie %+v", ex)
	}
}
