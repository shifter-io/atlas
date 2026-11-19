package build

import (
	"testing"

	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/snap"
)

// blockedTarget is a target result of tries tries, refused of them gateway
// refusals (proxy_connect) and blocked of them site blocks (http_status).
func blockedTarget(tries, refused, blocked float64) snap.TargetResult {
	return snap.TargetResult{Metrics: snap.Metrics{Completed: Num(tries),
		ErrorCategories: map[string]float64{"proxy_connect": refused, "http_status": blocked}}}
}

func blockedFlag(b model.Bool) string {
	if b == nil {
		return "null"
	}
	if *b {
		return "true"
	}
	return "false"
}

func TestNetworkBlocked(t *testing.T) {
	ok := blockedTarget(500, 5, 20)
	auth := blockedTarget(200, 0, 0)
	auth.Metrics.ErrorCategories = map[string]float64{"proxy_connect": 100, "proxy_auth": 90}
	old := blockedTarget(500, 500, 0)
	old.Metrics.ErrorCategories = nil
	cases := []struct {
		name    string
		targets map[string]snap.TargetResult
		want    string
	}{
		{"refused while the others are not", map[string]snap.TargetResult{"x": blockedTarget(200, 200, 0), "y": ok}, "true"},
		{"exactly the share", map[string]snap.TargetResult{"x": blockedTarget(100, 90, 10), "y": ok}, "true"},
		{"proxy_auth counts", map[string]snap.TargetResult{"x": auth, "y": ok}, "true"},
		{"just under the share", map[string]snap.TargetResult{"x": blockedTarget(1000, 899, 0), "y": ok}, "false"},
		{"a site block is not a refusal", map[string]snap.TargetResult{"x": blockedTarget(500, 0, 500), "y": ok}, "false"},
		{"too few tries", map[string]snap.TargetResult{"x": blockedTarget(99, 99, 0), "y": ok}, "null"},
		{"no error categories", map[string]snap.TargetResult{"x": old, "y": ok}, "null"},
		{"not measured", map[string]snap.TargetResult{"y": ok}, "null"},
		{"every target refused", map[string]snap.TargetResult{"x": blockedTarget(200, 200, 0), "y": blockedTarget(500, 250, 0)}, "null"},
		{"others under the minimum", map[string]snap.TargetResult{"x": blockedTarget(200, 200, 0), "y": blockedTarget(99, 0, 0)}, "null"},
		{"others just under half refused", map[string]snap.TargetResult{"x": blockedTarget(200, 200, 0), "y": blockedTarget(500, 249, 0)}, "true"},
	}
	for _, c := range cases {
		w := snap.EmptyWindow()
		w.Targets = c.targets
		if got := blockedFlag(networkBlocked(w, "x")); got != c.want {
			t.Errorf("%s: networkBlocked = %s, want %s", c.name, got, c.want)
		}
	}
}

// A group target's tries are its sources' tries: the others leave out the
// group, and a group's own sources, so no try counts twice.
func TestNetworkBlockedGroups(t *testing.T) {
	group := blockedTarget(400, 400, 0)
	group.Browser = &snap.BrowserResult{Sources: []string{"a", "b"}}
	w := snap.EmptyWindow()
	w.Targets = map[string]snap.TargetResult{"g": group, "a": blockedTarget(200, 200, 0), "b": blockedTarget(200, 200, 0),
		"y": blockedTarget(150, 0, 0)}
	for id, want := range map[string]string{"g": "true", "a": "null", "y": "false"} {
		if got := blockedFlag(networkBlocked(w, id)); got != want {
			t.Errorf("%s: networkBlocked = %s, want %s", id, got, want)
		}
	}
}

// A cell keeps its flag in its main engine's view and is null in another
// engine's, whose tries the flag does not count.
func TestNetworkBlockedEngineView(t *testing.T) {
	c := &TargetCell{NetworkBlocked: model.B(true), BrowserCell: &BrowserCell{Engine: model.S("camoufox"),
		engines: map[string]model.EngineBenchmark{}}}
	for engine, want := range map[string]string{"": "true", "camoufox": "true", "chrome": "null"} {
		if got := blockedFlag(viewCell(c, engine).NetworkBlocked); got != want {
			t.Errorf("engine %q: networkBlocked = %s, want %s", engine, got, want)
		}
	}
	netCell := &TargetCell{NetworkBlocked: model.B(false)}
	if got := blockedFlag(viewCell(netCell, "chrome").NetworkBlocked); got != "false" {
		t.Errorf("a network target in an engine view: networkBlocked = %s, want false", got)
	}
}
