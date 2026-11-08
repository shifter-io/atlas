package server_test

import (
	"fmt"
	"strings"
	"testing"
	"time"
)

// limits are one measurement's CI guard and staging budget (bench.CIGuard,
// bench.StagingBudgets).
type limits struct{ guard, staging time.Duration }

// verdict is the failure when last, the measurement kept, is over the guard
// (first is the one before it, the same when it was not retried) or else the
// warning when last is over the staging budget; "" when it is not.
func verdict(name string, l limits, first, last timing) (fail, warn string) {
	switch {
	case last.took > l.guard:
		return fmt.Sprintf("%s: p99 %v (%s), measured again %v (%s), both over its %v CI guard (staging budget %v)",
			name, first.took, first.path, last.took, last.path, l.guard, l.staging), ""
	case last.took > l.staging:
		return "", fmt.Sprintf("STAGING-BUDGET %s: p99 %v (%s), over its %v staging budget, within the %v CI guard",
			name, last.took, last.path, l.staging, l.guard)
	}
	return "", ""
}

func TestLatencyVerdict(t *testing.T) {
	l := limits{guard: 10 * time.Millisecond, staging: 2 * time.Millisecond}
	at := func(ms float64) timing { return timing{"/v1/x", time.Duration(ms * float64(time.Millisecond))} }
	for _, c := range []struct {
		first, last float64
		fail, warn  string
	}{
		{1, 1, "", ""},
		{2, 2, "", ""},
		{3, 3, "", "STAGING-BUDGET x: p99 3ms (/v1/x), over its 2ms staging budget, within the 10ms CI guard"},
		{12, 1.5, "", ""},
		{12, 4, "", "STAGING-BUDGET x: p99 4ms"},
		{12, 11, "x: p99 12ms (/v1/x), measured again 11ms (/v1/x), both over its 10ms CI guard (staging budget 2ms)", ""},
	} {
		fail, warn := verdict("x", l, at(c.first), at(c.last))
		if fail != c.fail || !strings.HasPrefix(warn, c.warn) || (c.warn == "") != (warn == "") {
			t.Errorf("%v then %v: fail %q, warn %q", c.first, c.last, fail, warn)
		}
	}
}
