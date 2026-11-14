package build

import (
	"math"
	"slices"
	"sort"

	"github.com/Zeus-Development/atlas/api/internal/jsnum"
	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/snap"
)

// recentWindows is, per range, the recent window and the longer one it is
// compared with for coverage and authenticity: the snapshot publishes no
// earlier period for them, so the latest part of a period is set against
// the whole (api/answers/changelog.md).
var recentWindows = map[string][2]string{"24h": {"24h", "7d"}, "7d": {"7d", "30d"}, "30d": {"7d", "30d"}}

// windowEntries is the performance, pool, coverage and authenticity entries
// of a measured network's headline product in one range.
func windowEntries(e *entry, c *Context, rangeID string) []ChangeEntry {
	sp, ws := e.Snap[e.P.PrimaryKind], c.Stats[e.P.Slug][rangeID]
	out := []ChangeEntry{}
	if sp == nil || ws == nil {
		return out
	}
	age := Num(0)
	if end := snap.Ms(sp.Window(rangeID).Period.End); !math.IsNaN(end) {
		age = asOfAge(c.GeneratedAt, end)
	}
	base := func(kind, id string) ChangeEntry { return measuredChange(e, kind, "chg-"+id+"-"+e.P.Slug, age) }
	out = append(out, performanceEntries(base, ws)...)
	return append(out, recentEntries(base, sp, rangeID)...)
}

// performanceEntries is a range's success, latency and pool moves.
func performanceEntries(base func(kind, id string) ChangeEntry, ws *WindowStats) []ChangeEntry {
	out := []ChangeEntry{}
	if d := ws.SuccessDeltaPts; d.OK() && math.Abs(d.F()) >= PerformanceSuccessPts {
		en := base("performance", "success").moved(d.F(), "success_rose", "success_fell", d > 0)
		out = append(out, en.withValues(ChangeValues{"metric": "success", "value": share4(ws.successEdges[1].F()),
			"previous": share4(ws.successEdges[0].F()), "delta": fixed1(d.F())}))
	}
	if d := ws.LatencyDeltaPct; d.OK() && math.Abs(d.F()) >= PerformanceLatencyPct {
		en := base("performance", "latency").moved(d.F(), "latency_rose", "latency_fell", d < 0)
		out = append(out, en.withValues(ChangeValues{"metric": "latency", "value": Num(jsnum.Round(ws.latencyEdges[1].F())),
			"previous": Num(jsnum.Round(ws.latencyEdges[0].F())), "delta": fixed1(d.F())}))
	}
	if d, edges := poolTrend(ws.Pool); !math.IsNaN(d) && math.Abs(d) >= PoolChangePct {
		en := base("pool", "pool").moved(d, "pool_grew", "pool_shrank", d > 0)
		out = append(out, en.withValues(ChangeValues{"uniqueIps": Num(jsnum.Round(edges[1].F())),
			"previousUniqueIps": Num(jsnum.Round(edges[0].F())), "deltaPct": fixed1(d)}))
	}
	return out
}

// recentEntries is the coverage and authenticity entries: the range's
// recent window set against the longer one (recentWindows).
func recentEntries(base func(kind, id string) ChangeEntry, sp *snap.Product, rangeID string) []ChangeEntry {
	out := []ChangeEntry{}
	pair := recentWindows[rangeID]
	recent, whole := sp.Window(pair[0]), sp.Window(pair[1])
	if !ranIn(recent) || !ranIn(whole) {
		return out
	}
	if lost, had := lostCountries(recent, whole); len(lost) > 0 {
		en := base("coverage", "coverage")
		en.Event, en.Severity = "countries_lost", "bad"
		out = append(out, en.withValues(ChangeValues{"countries": Num(had - len(lost)), "previousCountries": Num(had),
			"delta": Num(-len(lost)), "lostCountryCodes": lost, "comparedRange": pair[0]}))
	}
	if now, was := recent.Pool.GeoAccuracy, whole.Pool.GeoAccuracy; now.OK() && was.OK() && math.Abs((now-was).F()*100) >= AuthenticityPts {
		d := (now - was).F() * 100
		en := base("authenticity", "authenticity").moved(d, "geo_accuracy_rose", "geo_accuracy_fell", d > 0)
		out = append(out, en.withValues(ChangeValues{"value": share4(now.F()), "previous": share4(was.F()), "delta": fixed1(d)}))
	}
	return out
}

// poolTrend is the unique IPs per slot between the opening and closing fifth
// of the range's series, in percent (as latencyDeltaPct), and the two means;
// NaN with fewer than two slots or no IPs at the opening.
func poolTrend(points []model.SeriesPoint) (float64, trendEdges) {
	kept := []model.SeriesPoint{}
	for _, p := range points {
		if p.V.OK() {
			kept = append(kept, p)
		}
	}
	if len(kept) < 2 {
		return math.NaN(), trendEdges{nan, nan}
	}
	opening, closing := edgeMeans(kept)
	if !(opening > 0) {
		return math.NaN(), trendEdges{nan, nan}
	}
	return trendPct(kept), trendEdges{Num(opening), Num(closing)}
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
