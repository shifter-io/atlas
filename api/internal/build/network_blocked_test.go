package build

import (
	"testing"

	"github.com/shifter-io/atlas/api/internal/model"
	"github.com/shifter-io/atlas/api/internal/snap"
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

// blockedProduct is a product whose target x the gateway refuses in the
// last 24 hours (day) and whose 7d and 30d windows are long.
func blockedProduct(day, long snap.TargetResult) *snap.Product {
	window := func(x snap.TargetResult, y float64) snap.Window {
		w := snap.EmptyWindow()
		w.Targets = map[string]snap.TargetResult{"x": x, "y": blockedTarget(y, 0, 0)}
		return w
	}
	return &snap.Product{Windows: map[string]snap.Window{"24h": window(day, 500), "7d": window(long, 3500), "30d": window(long, 15000)}}
}

// NETWORK-BLOCKED-2: every range takes the last 24 hours' verdict, so a
// refusal that began inside a longer range marks it too, on /v1/targets and
// on the network answer's ranges alike; the range's own counts decide when
// the last 24 hours cannot tell or count another engine.
func TestNetworkBlockedEveryRange(t *testing.T) {
	chrome := func(r snap.TargetResult, engine string) snap.TargetResult {
		r.Browser = &snap.BrowserResult{Engine: engine}
		return r
	}
	cases := []struct {
		name string
		p    *snap.Product
		want map[string]string
	}{
		{"refused since two days", blockedProduct(blockedTarget(200, 200, 0), blockedTarget(700, 200, 0)),
			map[string]string{"24h": "true", "7d": "true", "30d": "true"}},
		{"refused no longer", blockedProduct(blockedTarget(200, 0, 0), blockedTarget(700, 650, 0)),
			map[string]string{"24h": "false", "7d": "false", "30d": "false"}},
		{"too few tries today", blockedProduct(blockedTarget(50, 50, 0), blockedTarget(700, 650, 0)),
			map[string]string{"24h": "null", "7d": "true", "30d": "true"}},
		{"another engine today", blockedProduct(chrome(blockedTarget(200, 200, 0), "chrome"), chrome(blockedTarget(700, 200, 0), "camoufox")),
			map[string]string{"24h": "true", "7d": "false", "30d": "false"}},
	}
	targets := []model.TargetMeta{{ID: "x"}, {ID: "y"}}
	for _, c := range cases {
		e := &entry{P: &model.Provider{Slug: "n"}, Snap: map[string]*snap.Product{"residential": c.p}}
		ans := &NetworkAnswer{Ranges: map[string]*RangeTotals{}}
		for r := range c.want {
			ans.Ranges[r] = &RangeTotals{Targets: []TargetTraffic{{Target: "x"}}}
		}
		rangesBlocked(ans, c.p)
		for r, want := range c.want {
			cell := targetNetworks([]*entry{e}, targets, "residential", r)[0].Cells["x"]
			if got := blockedFlag(cell.NetworkBlocked); got != want {
				t.Errorf("%s, %s: cell networkBlocked = %s, want %s", c.name, r, got, want)
			}
			if got := blockedFlag(ans.Ranges[r].Targets[0].NetworkBlocked); got != want {
				t.Errorf("%s, %s: ranges networkBlocked = %s, want %s", c.name, r, got, want)
			}
		}
	}
}
