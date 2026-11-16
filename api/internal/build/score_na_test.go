package build

import (
	"encoding/json"
	"math"
	"testing"

	"github.com/Zeus-Development/atlas/api/internal/snap"
	"github.com/Zeus-Development/atlas/api/internal/store"
	"github.com/Zeus-Development/atlas/api/internal/testscale"
)

// The calculator's reason texts (atlas calculated/scores.py,
// underload.py and window_scoring.py), as a payload carries them.
const (
	noPulses    = "No finished pulse batches in this window"
	fewPulses   = "Fewer finished pulse batches than the methodology requires"
	noLoadTest  = "No load test in this window"
	lateLoad    = "No load test before this window's light-load slots"
	censusLoad  = "Every load test in this window ran during a census (or its reference pulse did)"
	lightLoad   = "No light-load slot with enough successes in this window"
	fewPulseHit = "No pulse with enough successes in this window"
	censusPulse = "Every pulse with enough successes in this window ran during a census"
	noHTTPS     = "Required benchmark incomplete: generic_https"
	targetsOpen = "Required benchmark targets are incomplete"
	noPrice     = "Verified effective-dated pricing (lowest $/GB) is required"
	fewRequests = "Insufficient requests"
	lowCoverage = "Insufficient time coverage"
	expiredText = "Required measurement freshness deadline expired"
)

// nullScore is a payload's null balanced score with reasons, its components
// (each a value, nil for null, and a reason, "" for none) as given.
func nullScore(reasons []string, parts map[string][2]any) map[string]any {
	components := map[string]any{}
	for k, p := range parts {
		c := map[string]any{"value": p[0], "state": "incomplete", "reason": nil, "inputs": map[string]any{"peers": 1}}
		if p[1] != "" {
			c["reason"] = p[1]
		}
		components[k] = c
	}
	return map[string]any{"score": nil, "grade": nil, "rank": nil, "previous_rank": nil, "state": "incomplete",
		"reasons": reasons, "components": components}
}

// decodeScore is score decoded as the API decodes a window's score.
func decodeScore(t *testing.T, score map[string]any) snap.Score {
	t.Helper()
	b, err := json.Marshal(score)
	if err != nil {
		t.Fatal(err)
	}
	var s snap.Score
	if err := json.Unmarshal(b, &s); err != nil {
		t.Fatal(err)
	}
	return s
}

func TestEveryScoreReasonCode(t *testing.T) {
	t.Parallel()
	for _, c := range []struct {
		reasons []string
		want    string
	}{
		{[]string{noPulses}, NANoPulseTests},
		{[]string{fewPulses}, NANoPulseTests},
		{[]string{noLoadTest}, NANoLoadTest},
		{[]string{lateLoad}, NANoLoadTest},
		{[]string{censusLoad}, NANoLoadTest},
		{[]string{fewPulseHit}, NAFewSuccesses},
		{[]string{censusPulse}, NAFewSuccesses},
		{[]string{noLoadTest, fewPulseHit}, NAFewSuccesses},
		{[]string{noHTTPS}, NARequiredTarget},
		{[]string{targetsOpen}, NARequiredTarget},
		{[]string{noPrice}, NANoPrice},
		{[]string{fewRequests}, NAFewRequests},
		{[]string{lowCoverage}, NALowCoverage},
		{[]string{expiredText}, NAExpired},
		// Several reasons: the first code in precedence order, whatever
		// the order of the texts.
		{[]string{noHTTPS, noPrice, noLoadTest, noPulses}, NANoPulseTests},
		{[]string{expiredText, noHTTPS}, NARequiredTarget},
		{[]string{"uptime not ready", noPrice}, NANoPrice},
		// No matching reason keeps insufficient_data.
		{[]string{lightLoad}, NAInsufficient},
		{[]string{"uptime not ready"}, NAInsufficient},
		{nil, NAInsufficient},
	} {
		f := naFacts{measured: true, ran: true, priced: true, why: whyOf(decodeScore(t, nullScore(c.reasons, nil)))}
		for _, p := range []string{"score", "grade", "rank", "standing.rank", "network.score", "network.rank",
			"score.composite", "score.grade", "score.rank", "score.standing.rank"} {
			if got := f.code(p); got != c.want {
				t.Errorf("%q %s: %s, want %s", c.reasons, p, got, c.want)
			}
		}
	}
}

func TestABreakdownComponentHasItsOwnReason(t *testing.T) {
	t.Parallel()
	score := decodeScore(t, nullScore([]string{noHTTPS}, map[string][2]any{
		"uptime": {nil, noPulses}, "speed": {nil, noLoadTest}, "reliability": {nil, targetsOpen},
		"value": {nil, noPrice}, "coverage": {nil, ""}, "diversity": {80, ""}, "authenticity": {nil, "No exit IP with a DB-IP country"},
	}))
	if score.Components["speed"].Reason == nil || *score.Components["speed"].Reason != noLoadTest || len(score.Reasons) != 1 {
		t.Fatalf("decoded %+v", score)
	}
	all := naFacts{measured: true, ran: true, priced: true, why: whyOf(score)}
	pulsed := naFacts{measured: true, ran: true, priced: true, why: whyOf(decodeScore(t, nullScore(nil,
		map[string][2]any{"speed": {nil, fewPulseHit}})))}
	unpriced, idle := all, all
	unpriced.priced, idle.ran = false, false
	for _, c := range []struct {
		f          naFacts
		path, want string
	}{
		{all, "breakdown.uptime", NANoPulseTests},
		{all, "score.breakdown.speed", NANoLoadTest},
		{pulsed, "breakdown.speed", NAFewSuccesses},
		{all, "breakdown.reliability", NARequiredTarget},
		{all, "breakdown.value", NANoPrice},
		{all, "breakdown.coverage", NAInsufficient},
		{all, "breakdown.authenticity", NAInsufficient},
		{all, "score", NARequiredTarget},
		// The catalog and a window without a run keep their precedence.
		{unpriced, "breakdown.value", NACatalog},
		{idle, "breakdown.uptime", NANoRun},
		{idle, "score", NANoRun},
		// Other paths never take a score's reason.
		{all, "previousRank", NAInsufficient},
		{all, "captchaRate", NAInsufficient},
	} {
		if got := c.f.code(c.path); got != c.want {
			t.Errorf("%s: %s, want %s", c.path, got, c.want)
		}
	}
}

// A score with a value (retained, or rank null while alone in its kind) gives
// its rank no reason code from its reasons: insufficient_data stays.
func TestAScoreWithAValueKeepsInsufficientData(t *testing.T) {
	t.Parallel()
	s := decodeScore(t, nullScore([]string{expiredText}, map[string][2]any{"uptime": {90, noPulses}}))
	s.Score = 70
	f := naFacts{measured: true, ran: true, priced: true, why: whyOf(s)}
	for _, p := range []string{"rank", "score.rank", "breakdown.uptime"} {
		if got := f.code(p); got != NAInsufficient {
			t.Errorf("%s: %s", p, got)
		}
	}
	if w := whyOf(snap.EmptyScore()); w.code("score") != "" || headWhy(nil).code("score") != "" {
		t.Errorf("empty score: %+v", w)
	}
}

// net00's balanced score is null in every window with the calculator's
// reasons: its ranking row, home panel, network row and profile say why.
func TestABuildSaysWhyANetworksScoreIsEmpty(t *testing.T) {
	t.Parallel()
	in, err := testscale.Inputs(testscale.Shape{Networks: 2, Countries: 1, Exits: 3, Tail: 5})
	if err != nil {
		t.Fatal(err)
	}
	in.Snapshot = editSnapshot(t, in.Snapshot, func(doc map[string]any) {
		for _, p := range doc["products"].([]any) {
			if p := p.(map[string]any); p["provider"] == "net00" {
				for _, w := range p["windows"].(map[string]any) {
					w.(map[string]any)["scores"] = map[string]any{"balanced": nullScore([]string{noHTTPS, noPulses, noLoadTest}, map[string][2]any{
						"uptime": {nil, noPulses}, "speed": {nil, noLoadTest}, "reliability": {nil, targetsOpen}, "value": {nil, noPrice},
					})}
				}
			}
		}
		setPricing(doc, "net00", 2, math.NaN(), 2)
	})
	price := 2.0
	in.Pricing = append(in.Pricing, store.PricingRow{ProviderSlug: "net00", Category: "residential", PricePerGb: &price})
	res, err := Build(in)
	if err != nil {
		t.Fatal(err)
	}
	pg := res.Pages
	row := rankingRowOf(t, pg.Rankings["30d"], "net00")
	home := homeOf(t, pg, "30d")
	panel := homeNet(t, home, "net00")
	net := pg.Network["net00/residential"]
	for _, c := range []struct {
		of         *NAList
		path, want string
	}{
		{&row.NAList, "score", NANoPulseTests},
		{&row.NAList, "grade", NANoPulseTests},
		{&row.NAList, "rank", NANoPulseTests},
		{&row.NAList, "breakdown.speed", NANoLoadTest},
		{&row.NAList, "breakdown.reliability", NARequiredTarget},
		{&row.NAList, "breakdown.value", NANoPrice},
		{&panel.NAList, "score", NANoPulseTests},
		{&panel.NAList, "breakdown.uptime", NANoPulseTests},
		{&net.NAList, "network.score", NANoPulseTests},
		{&net.NAList, "network.rank", NANoPulseTests},
		{&net.Profile.NAList, "score.composite", NANoPulseTests},
		{&net.Profile.NAList, "score.breakdown.speed", NANoLoadTest},
		// net01 keeps its score: alone in its kind, its rank stays insufficient_data.
		{&rankingRowOf(t, pg.Rankings["30d"], "net01").NAList, "rank", NAInsufficient},
	} {
		if got := c.of.ReasonOf(c.path); got != c.want {
			t.Errorf("%s: %q, want %s (na %v)", c.path, got, c.want, c.of.NA)
		}
	}
}
