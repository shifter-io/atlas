package build

import (
	"encoding/json"
	"testing"

	"github.com/Zeus-Development/atlas/api/internal/testscale"
)

// attemptsNetwork's 24h window is IPRoyal's at 2026-11-14 23:10Z (#1117):
// 568,157 requests and 174,365 successes (0.307 of them), but a mean of the
// hourly time-slot success rates of 0.378, which an hour holding only 10,000
// pulse requests at 88% lifted as much as the hour of 264,000 daily-test
// requests at 12%. olderNetwork's snapshot has no request_success_rate.
const (
	attemptsNetwork = "net03"
	olderNetwork    = "net04"
)

// attemptsPerSuccess is the range's requests ÷ successes, the counts next to
// it in the answer and the rate every cost per success divides by, never
// 1 ÷ the time-slot success rate; a snapshot without request_success_rate
// keeps 1 ÷ success_rate (ATTEMPTS-REQUESTS-1, #1117).
func TestAttemptsPerSuccessIsRequestsPerSuccess(t *testing.T) {
	t.Parallel()
	in, err := testscale.WeekInputs(testscale.Shape{Networks: 18, Countries: 1, Exits: 3, Tail: 30, Browser: true})
	if err != nil {
		t.Fatal(err)
	}
	var doc map[string]any
	if err := json.Unmarshal(in.Snapshot.Payload, &doc); err != nil {
		t.Fatal(err)
	}
	older := 0.0
	for _, p := range doc["products"].([]any) {
		p := p.(map[string]any)
		m := p["windows"].(map[string]any)["24h"].(map[string]any)["metrics"].(map[string]any)
		switch p["provider"] {
		case attemptsNetwork:
			m["completed"], m["successes"], m["errors"] = 568157, 174365, 393792
			m["success_rate"], m["request_success_rate"] = 0.37844411720474674, 174365.0/568157
		case olderNetwork:
			delete(m, "request_success_rate")
			older = m["success_rate"].(float64)
		}
	}
	if in.Snapshot.Payload, err = json.Marshal(doc); err != nil {
		t.Fatal(err)
	}
	res, err := Build(in)
	if err != nil {
		t.Fatal(err)
	}
	ans := res.Pages.Network[attemptsNetwork+"/residential"]
	r, got := ans.Ranges["24h"], ans.Stats["24h"].AttemptsPerSuccess
	if !near(got, 568157.0/174365) || !near(got, r.Requests.F()/r.Successes.F()) {
		t.Errorf("attemptsPerSuccess %v, want requests %v ÷ successes %v = %v (not 1 ÷ 0.378 = 2.642)",
			got, r.Requests, r.Successes, 568157.0/174365)
	}
	if got := res.Pages.Network[olderNetwork+"/residential"].Stats["24h"].AttemptsPerSuccess; older <= 0 || !near(got, 1/older) {
		t.Errorf("older snapshot: attemptsPerSuccess %v, want 1 ÷ success_rate %v", got, older)
	}
}
