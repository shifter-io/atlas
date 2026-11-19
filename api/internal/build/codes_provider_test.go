package build

import (
	"testing"

	"github.com/shifter-io/atlas/api/internal/model"
)

// A stored use case or rejection reason is served as its code
// (PROFILE-TEXT-REMOVE-1): a use case that is no code is null, a rejection
// reason that is no code "other", and no reason null.
func TestStoredTextAsCodes(t *testing.T) {
	t.Parallel()
	for text, want := range map[string]string{"web_scraping": "web_scraping", " Web scraping ": "web_scraping",
		"AI-agents": "ai_agents", "scraping": "", "": ""} {
		if got := model.SV(codeOf(UseCases, text)); got != want {
			t.Errorf("use case %q: %q, want %q", text, got, want)
		}
	}
	reason := func(s string) *string { return &s }
	for _, c := range []struct {
		reason *string
		want   string
	}{{nil, ""}, {reason(" "), ""}, {reason("duplicate"), "duplicate"}, {reason("Collector outdated"), "collector_outdated"},
		{reason("The proxy refused us."), "other"}} {
		if got := model.SV(rejectionCode(c.reason)); got != c.want {
			t.Errorf("rejection %v: %q, want %q", c.reason, got, c.want)
		}
	}
}
