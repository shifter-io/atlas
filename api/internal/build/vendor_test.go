package build_test

import (
	"regexp"
	"testing"
)

// NO-VENDOR-NAME-1: no answer of a v4 build and not its dataset file names
// the IP data vendor, in any spelling or case (the server's
// TestNoAnswerNamesTheIPDataVendor scans the committed fixtures' builds).
func TestNoBuildAnswerNamesTheIPDataVendor(t *testing.T) {
	t.Parallel()
	vendor := regexp.MustCompile(`(?i)db[-_]?ip`)
	file, res := v4File(t)
	answers := netAnswers(t, res)
	answers["dataset file"] = file
	for name, b := range answers {
		if m := vendor.Find(b); m != nil {
			t.Errorf("%s names the IP data vendor: %q", name, m)
		}
	}
}
