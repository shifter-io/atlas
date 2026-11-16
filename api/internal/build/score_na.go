package build

import (
	"slices"
	"strings"

	"github.com/Zeus-Development/atlas/api/internal/snap"
)

// The reason codes of a null score, grade, rank or breakdown component
// (SCORE-REASONS-1): the calculator's stated reason, read from the score's
// reasons and the component's reason (api/answers/README.md, "Missing data").
const (
	// NANoPulseTests: no (or too few) finished pulse batches, so no uptime.
	NANoPulseTests = "no_pulse_tests"
	// NANoLoadTest: no load test usable for the latency under load (the
	// speed before SPEED-PULSE-1; older snapshots).
	NANoLoadTest = "no_load_test"
	// NARequiredTarget: a required benchmark target has no ready score.
	NARequiredTarget = "required_target_missing"
	// NANoPrice: no verified effective-dated price (value).
	NANoPrice = "no_price_evidence"
	// NAFewRequests: fewer requests than the methodology requires.
	NAFewRequests = "few_requests"
	// NALowCoverage: the requests cover too little of the window's time.
	NALowCoverage = "low_time_coverage"
	// NAExpired: the measurements are older than the score's validity.
	NAExpired = "expired"
)

// reasonRule is a code and the fragments of the calculator's reason texts
// it stands for (atlas calculated/scores.py, underload.py and
// window_scoring.py).
type reasonRule struct {
	code      string
	fragments []string
}

func (r reasonRule) matches(text string) bool {
	return slices.ContainsFunc(r.fragments, func(f string) bool { return strings.Contains(text, f) })
}

// scoreReasons in order of precedence: a score with several reasons takes
// the first code any of them matches.
var scoreReasons = []reasonRule{
	{NANoPulseTests, []string{"finished pulse batches"}},
	// The speed reads the pulses' light-load latency (SPEED-PULSE-1): no
	// slot with enough pulse successes outside a census.
	{NAFewSuccesses, []string{"No pulse with enough successes", "Every pulse with enough successes"}},
	{NANoLoadTest, []string{"No load test", "Every load test in this window ran during a census"}},
	{NARequiredTarget, []string{"Required benchmark"}},
	{NANoPrice, []string{"Verified effective-dated pricing"}},
	{NAFewRequests, []string{"Insufficient requests"}},
	{NALowCoverage, []string{"Insufficient time coverage"}},
	{NAExpired, []string{"freshness deadline expired"}},
}

// reasonCode is the first code of scoreReasons that one of texts matches,
// "" when none does.
func reasonCode(texts []string) string {
	for _, r := range scoreReasons {
		if slices.ContainsFunc(texts, r.matches) {
			return r.code
		}
	}
	return ""
}

// scoreWhy is the reason codes of a score's nulls ("" where no reason
// matches): score is the score's own, also its grade's and rank's, set only
// when the score is null; parts is each null breakdown component's, from the
// component's own reason.
type scoreWhy struct {
	score string
	parts map[string]string
}

// whyOf is s's scoreWhy.
func whyOf(s snap.Score) scoreWhy {
	w := scoreWhy{parts: map[string]string{}}
	if !s.Score.OK() {
		w.score = reasonCode(s.Reasons)
	}
	for k, c := range s.Components {
		if !c.Value.OK() && c.Reason != nil {
			w.parts[k] = reasonCode([]string{*c.Reason})
		}
	}
	return w
}

// headWhy is the scoreWhy of a network's headline score (its primary
// kind's 30-day balanced score, which the profile and the network row show);
// none for the all-networks answer or a network without a measured headline.
func headWhy(e *entry) scoreWhy {
	if e == nil || e.Snap[e.P.PrimaryKind] == nil {
		return scoreWhy{}
	}
	return whyOf(balancedScore(e.Snap[e.P.PrimaryKind].Window("30d")))
}

// code is the reason code of a score path pattern: score, grade, rank and
// standing.rank (also under network. and score., the profile's composite)
// have the score's, breakdown.<component> the component's; "" for any other
// path or when no reason matches.
func (w scoreWhy) code(p string) string {
	p = strings.TrimPrefix(strings.TrimPrefix(p, "network."), "score.")
	if k, ok := strings.CutPrefix(p, "breakdown."); ok {
		return w.parts[k]
	}
	switch p {
	case "score", "composite", "grade", "rank", "standing.rank":
		return w.score
	}
	return ""
}
