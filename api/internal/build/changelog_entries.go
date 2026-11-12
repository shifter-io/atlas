package build

import (
	"fmt"
	"maps"
	"math"
	"slices"
	"sort"
	"strings"

	"github.com/Zeus-Development/proxy-index/api/internal/jsnum"
	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/Zeus-Development/proxy-index/api/internal/snap"
	"github.com/Zeus-Development/proxy-index/api/internal/store"
)

// The incident summaries (api/answers/changelog.md): fixed sentences the
// website translates.
const (
	outageSummary        = "Requests stopped succeeding in the affected countries."
	outageSummaryOngoing = "Requests stopped succeeding in the affected countries; still ongoing."
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
	inc := &ChangeOutage{Severity: incidentSeverity(minutes, share), Summary: outageSummary,
		TroughSuccessRate: troughSuccess(sp, g.start, g.end), Impact: impact, DurationMinutes: Num(jsnum.Round(minutes)), IncidentFacts: facts}
	if g.ongoing {
		inc.Summary, inc.DurationMinutes = outageSummaryOngoing, nan
	}
	inc.Scope = fmt.Sprintf("%d countries", len(codes))
	if len(codes) == 1 {
		inc.Scope = countryName(c, sp, codes[0])
	}
	en := measuredChange(e, "incident", "chg-outage-"+sp.ID+"-"+g.id, asOfAge(c.GeneratedAt, g.start))
	en.Incident, en.Severity = inc, "bad"
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

// countryName is a country code's name: the product's 30d breakdown, else
// the country answers', else the code.
func countryName(c *Context, sp *snap.Product, code string) string {
	for _, b := range sp.Window("30d").Pool.CountriesBreakdown {
		if b.CountryCode == code && b.Country != "" {
			return b.Country
		}
	}
	if c.Pages != nil {
		if a := c.Pages.Country[strings.ToUpper(code)]; a != nil && a.Country != nil {
			return *a.Country
		}
	}
	return code
}

// measuredChange is an entry of e's with no title, detail or figure yet.
func measuredChange(e *entry, kind, id string, age Num) ChangeEntry {
	return ChangeEntry{ID: id, Kind: kind, Network: &ChangeNetwork{Slug: e.P.Slug, Name: e.P.Name}, Delta: nan,
		Severity: "info", AgoSeconds: age, Source: e.P.Source}
}

// figure sets en's delta (1 dp), unit and severity: good when the move is
// for the better.
func (en ChangeEntry) figure(delta float64, unit string, better bool) ChangeEntry {
	en.Delta, en.Unit, en.Severity = Num(jsnum.FixedNum(delta, 1)), unit, "bad"
	if better {
		en.Severity = "good"
	}
	return en
}

// moved is the title template for a move up or down.
func moved(delta float64, up, down string, v string) *Template {
	if delta > 0 {
		return tpl(up, map[string]string{"v1": v})
	}
	return tpl(down, map[string]string{"v1": v})
}

func abs1(x float64) string { return jsnum.ToFixed(math.Abs(x), 1) }

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
			steps, _ := priceSteps(rows[e.P.Slug+"/"+kind], e.Snap[kind], utcDay(generated))
			for i := 1; i < len(steps); i++ {
				prev, cur := steps[i-1], steps[i]
				age := math.Max(0, generated.Sub(cur.from).Seconds())
				if cur.rate == prev.rate || age > seconds {
					continue
				}
				pct := (cur.rate - prev.rate) / prev.rate * 100
				en := measuredChange(e, "price", "chg-price-"+e.P.Slug+"-"+kind+"-"+cur.from.Format("2006-01-02"), Num(age))
				en.Title = moved(pct, "{name} raised list pricing {v1}", "{name} cut list pricing {v1}", abs1(pct)+"%")
				en.Detail = tpl("List rate went from {v1} to {v2} per GB.", map[string]string{
					"v1": "$" + jsnum.ToFixed(prev.rate, 2), "v2": "$" + jsnum.ToFixed(cur.rate, 2)})
				en = en.withValues(ChangeValues{"fromRate": Num(prev.rate), "toRate": Num(cur.rate)})
				out = append(out, en.figure(pct, "%", pct < 0))
			}
		}
	}
	return out
}

// catalogKinds maps a site.changelog kind to the page's.
var catalogKinds = map[string]string{"pricing": "price"}

var severities = map[string]bool{"info": true, "good": true, "bad": true}

// catalogEntries is the site.changelog rows of the range's last seconds,
// each sentence its own template key.
func catalogEntries(c *Context, seconds float64) []ChangeEntry {
	out := []ChangeEntry{}
	if c.Dataset == nil {
		return out
	}
	for _, r := range c.Dataset.Changelog {
		if !r.AgoSeconds.OK() || r.AgoSeconds.F() > seconds {
			continue
		}
		en := ChangeEntry{ID: r.ID, Kind: r.Kind, Title: tpl(r.Headline, nil), Delta: r.Delta, Unit: r.Unit,
			Severity: r.Severity, AgoSeconds: r.AgoSeconds, Source: r.Source}
		if k, ok := catalogKinds[r.Kind]; ok {
			en.Kind = k
		}
		if r.Detail != "" {
			en.Detail = tpl(r.Detail, nil)
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
