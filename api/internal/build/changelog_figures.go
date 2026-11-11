package build

import (
	"math"
	"sort"
	"strings"

	"github.com/Zeus-Development/proxy-index/api/internal/jsnum"
	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/Zeus-Development/proxy-index/api/internal/snap"
)

// recentWindows is, per range, the recent window and the longer one it is
// compared with for coverage and authenticity: the snapshot publishes no
// earlier period for them, so the latest part of a period is set against
// the whole (api/answers/changelog.md).
var recentWindows = map[string][2]string{"24h": {"24h", "7d"}, "7d": {"7d", "30d"}, "30d": {"7d", "30d"}}

// notSeenIn is the coverage detail of each recent window.
var notSeenIn = map[string]string{"24h": "Not seen in the last 24 hours: {v1}.", "7d": "Not seen in the last 7 days: {v1}."}

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
		en := base("performance", "success")
		en.Title = moved(d.F(), "{name} success rate rose {v1} points", "{name} success rate fell {v1} points", abs1(d.F()))
		en.Detail = tpl("Success rate is now {v1}.", map[string]string{"v1": jsnum.Pct(ws.SuccessRate.F(), 1)})
		out = append(out, en.figure(d.F(), "pts", d > 0))
	}
	if d := ws.LatencyDeltaPct; d.OK() && math.Abs(d.F()) >= PerformanceLatencyPct {
		en := base("performance", "latency")
		en.Title = moved(d.F(), "{name} median latency rose {v1}", "{name} median latency fell {v1}", abs1(d.F())+"%")
		en.Detail = tpl("Median latency is now {v1} ms.", map[string]string{"v1": jsnum.Locale(jsnum.Round(ws.LatencyP50.F()))})
		out = append(out, en.figure(d.F(), "%", d < 0))
	}
	if d := poolTrend(ws.Pool); !math.IsNaN(d) && math.Abs(d) >= PoolChangePct {
		en := base("pool", "pool")
		en.Title = moved(d, "{name} pool grew {v1}", "{name} pool shrank {v1}", abs1(d)+"%")
		if ws.UniqueIps.OK() {
			en.Detail = tpl("{v1} unique IPs in this window.", map[string]string{"v1": jsnum.Locale(ws.UniqueIps.F())})
		}
		out = append(out, en.figure(d, "%", d > 0))
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
	if lost := lostCountries(recent, whole); len(lost) > 0 {
		en := base("coverage", "coverage")
		key := "{name} lost {v1} countries"
		if len(lost) == 1 {
			key = "{name} lost {v1} country"
		}
		en.Title = tpl(key, map[string]string{"v1": jsnum.Locale(float64(len(lost)))})
		en.Detail = tpl(notSeenIn[pair[0]], map[string]string{"v1": strings.Join(lost, ", ")})
		out = append(out, en.figure(-float64(len(lost)), "countries", false))
	}
	if now, was := recent.Pool.GeoAccuracy, whole.Pool.GeoAccuracy; now.OK() && was.OK() && math.Abs((now-was).F()*100) >= AuthenticityPts {
		d := (now - was).F() * 100
		en := base("authenticity", "authenticity")
		en.Title = moved(d, "{name} geo accuracy rose {v1} points", "{name} geo accuracy fell {v1} points", abs1(d))
		en.Detail = tpl("{v1} of exit IPs are in the requested country.", map[string]string{"v1": jsnum.Pct(now.F(), 1)})
		out = append(out, en.figure(d, "pts", d > 0))
	}
	return out
}

// poolTrend is the unique IPs per slot between the opening and closing fifth
// of the range's series, in percent (as latencyDeltaPct); NaN with fewer
// than two slots or no IPs at the opening.
func poolTrend(points []model.SeriesPoint) float64 {
	kept := []model.SeriesPoint{}
	for _, p := range points {
		if p.V.OK() {
			kept = append(kept, p)
		}
	}
	if len(kept) < 2 || !(mean(values(head(kept, edge(len(kept))))) > 0) {
		return math.NaN()
	}
	return trendPct(kept)
}

// lostCountries is the names of the countries with IPs in the whole window
// and none in the recent one, by name; none when either has no breakdown.
func lostCountries(recent, whole snap.Window) []string {
	seen := map[string]bool{}
	for _, b := range recent.Pool.CountriesBreakdown {
		seen[b.CountryCode] = seen[b.CountryCode] || b.Ips.OK() && b.Ips > 0
	}
	if len(seen) == 0 {
		return nil
	}
	var lost []string
	for _, b := range whole.Pool.CountriesBreakdown {
		if b.Ips.OK() && b.Ips > 0 && !seen[b.CountryCode] {
			name := b.Country
			if name == "" {
				name = b.CountryCode
			}
			lost = append(lost, name)
		}
	}
	sort.Strings(lost)
	return lost
}
