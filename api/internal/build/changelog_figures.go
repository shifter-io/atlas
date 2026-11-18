package build

import (
	"math"
	"slices"
	"sort"

	"github.com/shifter-io/atlas/api/internal/jsnum"
	"github.com/shifter-io/atlas/api/internal/snap"
)

// recentWindows is, per range, the recent window and the longer one it is
// compared with for coverage and authenticity: the snapshot publishes no
// earlier period for them, so the latest part of a period is set against
// the whole (api/answers/changelog.md).
var recentWindows = map[string][2]string{"24h": {"24h", "7d"}, "7d": {"7d", "30d"}, "30d": {"7d", "30d"}}

// windowEntries is the performance, pool, coverage and authenticity entries
// of a measured network's headline product in one range. Each is dated at
// the end of the newest slot it compares (a performance or pool entry) or of
// the recent window (coverage, authenticity); at the window's end when that
// is unknown.
func windowEntries(e *entry, c *Context, rangeID string) []ChangeEntry {
	sp, ws := e.Snap[e.P.PrimaryKind], c.Stats[e.P.Slug][rangeID]
	out := []ChangeEntry{}
	if sp == nil || ws == nil {
		return out
	}
	w := sp.Window(rangeID)
	base := func(kind, id string, at float64) ChangeEntry {
		if !(at > 0) {
			at = snap.Ms(w.Period.End)
		}
		age := Num(0)
		if !math.IsNaN(at) {
			age = asOfAge(c.GeneratedAt, at)
		}
		return measuredChange(e, kind, "chg-"+id+"-"+e.P.Slug, age)
	}
	out = append(out, performanceEntries(base, ws)...)
	out = append(out, poolEntries(base, w, rangeID)...)
	return append(out, recentEntries(base, sp, rangeID)...)
}

// entryBase makes a figure entry of kind and id dated at (ms; the window's
// end when not above 0).
type entryBase func(kind, id string, at float64) ChangeEntry

// performanceEntries is a range's success and latency moves.
func performanceEntries(base entryBase, ws *WindowStats) []ChangeEntry {
	out := []ChangeEntry{}
	if d := ws.SuccessDeltaPts; d.OK() && math.Abs(d.F()) >= PerformanceSuccessPts {
		en := base("performance", "success", ws.successAt).moved(d.F(), "success_rose", "success_fell", d > 0)
		out = append(out, en.withValues(ChangeValues{"metric": "success", "value": share4(ws.successEdges[1].F()),
			"previous": share4(ws.successEdges[0].F()), "delta": fixed1(d.F())}))
	}
	if d := ws.LatencyDeltaPct; d.OK() && !ws.latencyCarried && math.Abs(d.F()) >= PerformanceLatencyPct {
		en := base("performance", "latency", ws.latencyAt).moved(d.F(), "latency_rose", "latency_fell", d < 0)
		values := ChangeValues{"metric": "latency", "value": Num(jsnum.Round(ws.latencyEdges[1].F())),
			"previous": Num(jsnum.Round(ws.latencyEdges[0].F())), "delta": fixed1(d.F())}
		if b := ws.LatencyDeltaBasis; b != nil {
			values["basis"] = *b
		}
		out = append(out, en.withValues(values))
	}
	return out
}

// poolEntries is a range's pool move, from complete sides only (poolChange).
func poolEntries(base entryBase, w snap.Window, rangeID string) []ChangeEntry {
	d, edges, at := poolChange(w, rangeID)
	if math.IsNaN(d) || math.Abs(d) < PoolChangePct {
		return []ChangeEntry{}
	}
	en := base("pool", "pool", at).moved(d, "pool_grew", "pool_shrank", d > 0)
	return []ChangeEntry{en.withValues(ChangeValues{"uniqueIps": Num(jsnum.Round(edges[1].F())),
		"previousUniqueIps": Num(jsnum.Round(edges[0].F())), "deltaPct": fixed1(d)})}
}

// recentEntries is the coverage and authenticity entries: the range's
// recent window set against the longer one (recentWindows).
func recentEntries(base entryBase, sp *snap.Product, rangeID string) []ChangeEntry {
	out := []ChangeEntry{}
	pair := recentWindows[rangeID]
	recent, whole := sp.Window(pair[0]), sp.Window(pair[1])
	if !ranIn(recent) || !ranIn(whole) {
		return out
	}
	if lost, had := lostCountries(recent, whole); len(lost) > 0 {
		en := base("coverage", "coverage", snap.Ms(recent.Period.End))
		en.Event, en.Severity = "countries_lost", "bad"
		out = append(out, en.withValues(ChangeValues{"countries": Num(had - len(lost)), "previousCountries": Num(had),
			"delta": Num(-len(lost)), "lostCountryCodes": lost, "comparedRange": pair[0]}))
	}
	if now, was := recent.Pool.GeoAccuracy, whole.Pool.GeoAccuracy; now.OK() && was.OK() && math.Abs((now-was).F()*100) >= AuthenticityPts {
		d := (now - was).F() * 100
		en := base("authenticity", "authenticity", snap.Ms(recent.Period.End)).moved(d, "geo_accuracy_rose", "geo_accuracy_fell", d > 0)
		out = append(out, en.withValues(ChangeValues{"value": share4(now.F()), "previous": share4(was.F()), "delta": fixed1(d)}))
	}
	return out
}

// lostCountries is the ISO codes, sorted, of the countries with IPs in the
// whole window and none in the recent one, and how many countries had IPs
// in the whole window; none when either has no breakdown.
func lostCountries(recent, whole snap.Window) (lost []string, had int) {
	seen := map[string]bool{}
	for _, b := range recent.Pool.CountriesBreakdown {
		seen[b.CountryCode] = seen[b.CountryCode] || b.Ips.OK() && b.Ips > 0
	}
	if len(seen) == 0 {
		return nil, 0
	}
	counted := map[string]bool{}
	for _, b := range whole.Pool.CountriesBreakdown {
		if !(b.Ips.OK() && b.Ips > 0) {
			continue
		}
		if !counted[b.CountryCode] {
			counted[b.CountryCode], had = true, had+1
		}
		if !seen[b.CountryCode] && !slices.Contains(lost, b.CountryCode) {
			lost = append(lost, b.CountryCode)
		}
	}
	sort.Strings(lost)
	return lost, had
}
