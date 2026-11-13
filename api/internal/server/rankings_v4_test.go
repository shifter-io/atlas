package server

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/build"
)

// rankingRowKeys are the fields every ranking row carries (RANKINGS-ROWS-1).
var rankingRowKeys = []string{"position", "slug", "name", "kind", "kinds", "tagline", "rank", "peers", "standing", "score",
	"listRate", "pricePerGb", "pricePerGbAtScale", "priceLabel", "trial", "hasTrial", "trialKind", "authenticity", "uptimePct",
	"downtimeMinutes", "requests", "latencyP90", "latencyP99", "kycVerified", "consentDisclosed", "stickinessAdvertised",
	"stickinessHoldRate", "blacklistHitRate", "valueRankAllTargets", "trend", "trendT", "browserOverall"}

// rankingMeasuredKeys are the row fields a measured network of the v4
// fixtures has a value for, beside every window figure but observedDays.
var rankingMeasuredKeys = []string{"standing", "requests", "latencyP90", "latencyP99"}

// rankingsServed checks a v4 build's ranking answers: each is under the
// answer cap, carries the answer's summaries and every row field, a measured
// row with a value for its figures, and the kind answers list rows of their
// kind only. It logs each answer's size.
func rankingsServed(t *testing.T, answers map[string][]byte) {
	t.Helper()
	n := 0
	for path, body := range answers {
		if !strings.HasPrefix(path, "/v1/rankings/") {
			continue
		}
		n++
		t.Logf("%s: %d bytes", path, len(body))
		if len(body) > build.MaxAnswerBytes {
			t.Errorf("%s: %d bytes, over the cap", path, len(body))
		}
		var ans struct {
			Targets, Weights, Profiles, Fleet, Cost json.RawMessage
			Networks                                []map[string]any
		}
		if err := json.Unmarshal(body, &ans); err != nil {
			t.Fatalf("%s: %v", path, err)
		}
		if ans.Targets == nil || ans.Weights == nil || ans.Profiles == nil || ans.Fleet == nil || ans.Cost == nil {
			t.Errorf("%s: an answer summary is missing", path)
		}
		_, kind, filtered := strings.Cut(strings.TrimPrefix(path, "/v1/rankings/"), "/")
		for _, row := range ans.Networks {
			rankingRow(t, path, row)
			if filtered && row["kind"] != kind {
				t.Errorf("%s: a %v row", path, row["kind"])
			}
		}
	}
	if n != 3*5 {
		t.Errorf("%d ranking answers, want 15", n)
	}
}

func rankingRow(t *testing.T, path string, row map[string]any) {
	t.Helper()
	keys := append(append([]string{}, rankingRowKeys...), windowFigureKeys...)
	for _, k := range keys {
		if _, ok := row[k]; !ok {
			t.Errorf("%s %v: no %s", path, row["slug"], k)
		}
	}
	if row["latencyNote"] == nil {
		return
	}
	for _, k := range append(append([]string{}, rankingMeasuredKeys...), windowFigureKeys...) {
		if row[k] == nil && k != "observedDays" {
			t.Errorf("%s %v: %s is null", path, row["slug"], k)
		}
	}
}
