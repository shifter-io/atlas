package store_test

import (
	"encoding/json"
	"strconv"
	"testing"
	"time"

	"github.com/shifter-io/atlas/api/internal/build"
)

// rankedHead is v4Head as of asOf with shifter's and acme's 30d balanced
// ranks.
func rankedHead(t *testing.T, asOf time.Time, shifter, acme int) []byte {
	t.Helper()
	var doc map[string]any
	if err := json.Unmarshal(v4Head(t, 0), &doc); err != nil {
		t.Fatal(err)
	}
	doc["as_of"] = asOf.Format(time.RFC3339)
	for _, raw := range doc["networks"].([]any) {
		n := raw.(map[string]any)
		rank := shifter
		if n["provider"] == "acme" {
			rank = acme
		}
		w := n["windows"].(map[string]any)["30d"].(map[string]any)
		scores, _ := w["scores"].(map[string]any)
		if scores == nil {
			scores = map[string]any{}
			w["scores"] = scores
		}
		balanced, _ := scores["balanced"].(map[string]any)
		if balanced == nil {
			balanced = map[string]any{}
			scores["balanced"] = balanced
		}
		balanced["rank"], balanced["previous_rank"] = rank, rank
	}
	out, err := json.Marshal(doc)
	if err != nil {
		t.Fatal(err)
	}
	return out
}

// CHANGELOG-REAL-1 (#1314): the load reads each network's rank in the kept
// published builds of the 30 days before the current one, oldest first
// (not one 40 days old, not an unpublished one), and the changelog's movers
// compare the current rank with each range's oldest: shifter rose from 2nd
// to 1st over 7 and 30 days. The snapshot's previous_rank, the build just
// before, shows no move.
func TestMoversCompareTheRankOfTheRangesOldestKeptBuild(t *testing.T) {
	t.Parallel()
	db, _ := networkLists(t)
	now := time.Date(2026, 11, 17, 3, 5, 0, 0, time.UTC)
	db.Publish(t, rankedHead(t, now.Add(-40*24*time.Hour), 9, 9))
	db.Publish(t, rankedHead(t, now.Add(-3*24*time.Hour), 2, 1))
	unpublished := db.Publish(t, rankedHead(t, now.Add(-2*24*time.Hour), 5, 5))
	db.Exec(t, "UPDATE calculation_builds SET published = false WHERE build_id = "+strconv.FormatInt(unpublished, 10))
	db.Publish(t, rankedHead(t, now.Add(-4*time.Hour), 1, 2))
	db.Publish(t, rankedHead(t, now, 1, 2))
	in := load(t, db)
	var got []string
	for _, r := range in.Ranks {
		got = append(got, r.AsOf.UTC().Format("01-02T15")+" "+r.Network+" "+strconv.Itoa(int(r.Rank)))
	}
	want := []string{"11-14T03 acme:residential:network 1", "11-14T03 shifter:residential:network 2",
		"11-16T23 acme:residential:network 2", "11-16T23 shifter:residential:network 1"}
	if len(got) != len(want) {
		t.Fatalf("ranks %v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("rank %d is %q, want %q", i, got[i], want[i])
		}
	}
	res, err := build.Build(in)
	if err != nil {
		t.Fatal(err)
	}
	for rangeID, moves := range map[string]int{"24h": 0, "7d": 2, "30d": 2} {
		m := res.Pages.Changelog[rangeID].Movers
		if len(m) != moves {
			t.Errorf("%s movers %+v, want %d", rangeID, m, moves)
			continue
		}
		if moves > 0 && (m[0].Slug != "acme" || m[0].Delta != -1 || m[1].Slug != "shifter" || m[1].PreviousRank != 2 || m[1].Delta != 1) {
			t.Errorf("%s movers %+v: want acme down one, shifter up one from 2nd", rangeID, m)
		}
	}
}
