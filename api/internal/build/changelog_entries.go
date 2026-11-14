package build

import (
	"maps"
	"math"
	"slices"
	"sort"

	"github.com/Zeus-Development/atlas/api/internal/jsnum"
	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/snap"
	"github.com/Zeus-Development/atlas/api/internal/store"
)

// outageGroup is a product's outages that overlap in time: one incident.
type outageGroup struct {
	id         string  // the first outage's started_at
	start, end float64 // ms; an ongoing outage ends at the build's time
	ongoing    bool
	outages    []snap.Outage
}

// outageGroups merges a product's outages that overlap into incidents,
// oldest first.
func outageGroups(outages []snap.Outage, asOf float64) []outageGroup {
	sorted := slices.Clone(outages)
	sort.SliceStable(sorted, func(i, j int) bool { return snap.Ms(sorted[i].StartedAt) < snap.Ms(sorted[j].StartedAt) })
	var out []outageGroup
	for _, o := range sorted {
		start, end := snap.Ms(o.StartedAt), asOf
		if math.IsNaN(start) {
			continue
		}
		if o.EndedAt != nil {
			end = outageEnd(o)
		}
		if n := len(out); n > 0 && start <= out[n-1].end {
			g := &out[n-1]
			g.end, g.ongoing = math.Max(g.end, end), g.ongoing || o.EndedAt == nil
			g.outages = append(g.outages, o)
			continue
		}
		out = append(out, outageGroup{id: o.StartedAt, start: start, end: end, ongoing: o.EndedAt == nil, outages: []snap.Outage{o}})
	}
	return out
}

// incidentSeverity is minor, moderate or major by the incident's length and
// the share of tested countries it took.
func incidentSeverity(minutes, share float64) string {
	switch {
	case minutes > IncidentMajorMinutes || share > IncidentMajorShare:
		return "major"
	case minutes < IncidentMinorMinutes && share < IncidentMinorShare:
		return "minor"
	}
	return "moderate"
}

// incidentEntries is every incident of e's products in the range's last
// seconds.
func incidentEntries(e *entry, c *Context, seconds float64) []ChangeEntry {
	asOf := snap.Ms(c.GeneratedAt)
	out := []ChangeEntry{}
	for _, kind := range slices.Sorted(maps.Keys(e.Snap)) {
		sp := e.Snap[kind]
		tested := nan
		if up := sp.Window("30d").Uptime; up != nil {
			tested = up.Countries
		}
		for _, g := range outageGroups(sp.Outages, asOf) {
			if g.end < asOf-seconds*1000 || g.start > asOf {
				continue
			}
			out = append(out, incidentEntry(e, c, sp, g, tested))
		}
	}
	return out
}

func incidentEntry(e *entry, c *Context, sp *snap.Product, g outageGroup, tested Num) ChangeEntry {
	minutes, facts := (g.end-g.start)/60000, incidentFacts(g.outages)
	codes := facts.CountryCodes
	impact, share := nan, 0.0
	if tested.OK() && tested > 0 {
		impact = Num(math.Min(1, float64(len(codes))/tested.F()))
		share = impact.F()
	}
	inc := &ChangeOutage{Severity: incidentSeverity(minutes, share),
		TroughSuccessRate: troughSuccess(sp, g.start, g.end), Impact: impact, DurationMinutes: Num(jsnum.Round(minutes)), IncidentFacts: facts}
	if g.ongoing {
		inc.DurationMinutes = nan
	}
	en := measuredChange(e, "incident", "chg-outage-"+sp.ID+"-"+g.id, asOfAge(c.GeneratedAt, g.start))
	en.Event, en.Incident, en.Severity = "incident", inc, "bad"
	if inc.Severity == "minor" {
		en.Severity = "info"
	}
	return en.withValues(nil)
}

// troughSuccess is the lowest success rate of a series slot that overlaps
// [from, to), from the finest window that has one; NaN when none does.
func troughSuccess(sp *snap.Product, from, to float64) Num {
	for _, w := range []struct {
		id   string
		step float64
	}{{"24h", 5 * 60000}, {"7d", 86400000}, {"30d", 86400000}} {
		low := nan
		for _, row := range sp.Window(w.id).Series {
			at := snap.Ms(row.At)
			if at < to && at+w.step > from && row.SuccessRate.OK() && (!low.OK() || row.SuccessRate < low) {
				low = row.SuccessRate
			}
		}
		if low.OK() {
			return low
		}
	}
	return nan
}

// measuredChange is an entry of e's with no event or figures yet.
func measuredChange(e *entry, kind, id string, age Num) ChangeEntry {
	return ChangeEntry{ID: id, Kind: kind, Network: &ChangeNetwork{Slug: e.P.Slug, Name: e.P.Name},
		Severity: "info", AgoSeconds: age, Source: e.P.Source}
}

// moved sets en's event, the up or the down code by the move's sign, and its
// severity: good when the move is for the better.
func (en ChangeEntry) moved(delta float64, up, down string, better bool) ChangeEntry {
	en.Event, en.Severity = down, "bad"
	if delta > 0 {
		en.Event = up
	}
	if better {
		en.Severity = "good"
	}
	return en
}

// fixed1 is a move's figure, to 1 dp.
func fixed1(x float64) Num { return Num(jsnum.FixedNum(x, 1)) }

// priceEntries is every list rate change of site.pricing_history in the
// range's last seconds.
func priceEntries(c *Context, seconds float64) []ChangeEntry {
	generated, ok := snap.ParseTime(c.GeneratedAt)
	out := []ChangeEntry{}
	if !ok || c.Inputs == nil {
		return out
	}
	rows := map[string][]store.PricingHistoryRow{}
	for _, r := range c.Inputs.PricingHistory {
		rows[r.ProviderSlug+"/"+r.Category] = append(rows[r.ProviderSlug+"/"+r.Category], r)
	}
	for _, e := range c.Entries {
		for _, kind := range model.ProxyKinds {
			steps := priceSteps(rows[e.P.Slug+"/"+kind], e.Snap[kind], utcDay(generated))
			for i := 1; i < len(steps); i++ {
				prev, cur := steps[i-1], steps[i]
				age := math.Max(0, generated.Sub(cur.from).Seconds())
				if cur.rate == prev.rate || age > seconds {
					continue
				}
				pct := (cur.rate - prev.rate) / prev.rate * 100
				en := measuredChange(e, "price", "chg-price-"+e.P.Slug+"-"+kind+"-"+cur.from.Format("2006-01-02"), Num(age))
				en = en.moved(pct, "price_raised", "price_cut", pct < 0)
				out = append(out, en.withValues(ChangeValues{"fromRate": Num(prev.rate), "toRate": Num(cur.rate), "deltaPct": fixed1(pct)}))
			}
		}
	}
	return out
}

// catalogKinds maps a site.changelog kind to the page's.
var catalogKinds = map[string]string{"pricing": "price"}

var severities = map[string]bool{"info": true, "good": true, "bad": true}

// catalogEntries is the site.changelog rows of the range's last seconds:
// their kind, network and severity, without the row's English headline and
// detail (API-PROSE-REMOVAL-1, #962) or its delta and unit (#990); their
// values are null (catalog).
func catalogEntries(c *Context, seconds float64) []ChangeEntry {
	out := []ChangeEntry{}
	if c.Dataset == nil {
		return out
	}
	for _, r := range c.Dataset.Changelog {
		if !r.AgoSeconds.OK() || r.AgoSeconds.F() > seconds {
			continue
		}
		en := ChangeEntry{ID: r.ID, Kind: r.Kind, Event: "catalog", Severity: r.Severity, AgoSeconds: r.AgoSeconds, Source: r.Source}
		if k, ok := catalogKinds[r.Kind]; ok {
			en.Kind = k
		}
		if !severities[en.Severity] {
			en.Severity = "info"
		}
		if r.ProviderSlug != nil {
			en.Network = &ChangeNetwork{Slug: *r.ProviderSlug, Name: model.SV(r.ProviderName)}
		}
		out = append(out, en.withValues(nil))
	}
	return out
}
