package build

import (
	"bytes"
	"encoding/json"
	"testing"

	"github.com/Zeus-Development/atlas/api/internal/testscale"
)

// No answer, not the coverage report (its na reasons) and not the dataset
// carries all_similar (SUCCESS-TIE-1): the
// small network build's tied places and the week's browser targets score a
// shared first place, 10, instead. The place rows of every list are checked
// by TestNetworkListRowsHaveTheirPlaceScore.
func TestNoAnswerCarriesAllSimilar(t *testing.T) {
	t.Parallel()
	small, _ := smallNetBuild(t)
	for name, res := range map[string]*Result{"small": small, "week": targetsScaleBuild(t, testscale.WeekInputs)} {
		bodies := map[string]any{"dataset": res.Dataset, "coverage": res.Coverage}
		for path, body := range res.Pages.Registered {
			bodies[path] = body
		}
		for path, body := range bodies {
			raw, err := json.Marshal(body)
			if err != nil {
				t.Fatal(err)
			}
			if bytes.Contains(raw, []byte("all_similar")) {
				t.Errorf("%s %s carries all_similar", name, path)
			}
		}
	}
}

// A tight race is a shared first place: 10, rank 1 of M for every ranked
// network, as calculated/target_scores.py's metric_scores gives it.
func TestATightRaceIsASharedFirstPlace(t *testing.T) {
	t.Parallel()
	scores := metricScores([]ratedRate{{0.91, 10000}, {0.9, 10000}, {0.895, 10000}, {0.2, 50}})
	for i, s := range scores[:3] {
		if s.Score != 10 || s.Rank != 1 || s.RankedOf != 3 || s.Status != scoreRanked {
			t.Errorf("network %d: %+v, want 10, rank 1 of 3", i, s)
		}
	}
	if s := scores[3]; s.Status != notEnoughData || s.Score.OK() {
		t.Errorf("the network under 100 requests: %+v", s)
	}
}
