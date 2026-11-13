package build

import (
	"math"
	"slices"
	"time"

	"github.com/Zeus-Development/atlas/api/internal/jsnum"
	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/snap"
)

// ChangeValues is an entry's numbers and codes (CHANGELOG-VALUES-1): only
// the keys of its kind (changeValueKeys), a number, a code or null.
type ChangeValues map[string]any

// changeValueKeys is each entry kind's values keys (api/answers/changelog.md).
var changeValueKeys = map[string][]string{
	"pool":         {"uniqueIps", "previousUniqueIps", "deltaPct"},
	"price":        {"fromRate", "toRate", "plan"},
	"performance":  {"metric", "value", "previous", "delta"},
	"coverage":     {"countries", "previousCountries", "delta"},
	"authenticity": {"value", "previous", "delta"},
	"methodology":  {"fromId", "toId"},
}

// changeValueNA is the reason of a null values key: a price change's plan
// is never published (site.pricing_history names no plan), every other key
// is null only on a site.changelog row, which carries sentences, not values.
func changeValueNA(key string) string {
	if key == "plan" {
		return NANotPublished
	}
	return NACatalog
}

// withValues sets en's values to v's keys of en's kind, each missing or
// NaN one null with its reason in en's naReasons.
func (en ChangeEntry) withValues(v ChangeValues) ChangeEntry {
	en.Values, en.NAReasons = ChangeValues{}, nil
	for _, k := range changeValueKeys[en.Kind] {
		x, ok := v[k]
		if n, isNum := x.(Num); ok && x != nil && (!isNum || n.OK()) {
			en.Values[k] = x
			continue
		}
		en.Values[k] = nil
		if en.NAReasons == nil {
			en.NAReasons = map[string]string{}
		}
		en.NAReasons["values."+k] = changeValueNA(k)
	}
	return en
}

// edgeMeans is the mean of the opening and of the closing fifth of points,
// the two figures trendPts and trendPct compare.
func edgeMeans(points []model.SeriesPoint) (opening, closing float64) {
	e := edge(len(points))
	return mean(values(head(points, e))), mean(values(tail(points, e)))
}

// share4 is a 0-1 share to 4 dp.
func share4(x float64) Num { return Num(jsnum.FixedNum(x, 4)) }

// outageEnd is a closed outage's end in ms: its ended_at, else its start
// plus its duration.
func outageEnd(o snap.Outage) float64 {
	if end := snap.Ms(*o.EndedAt); !math.IsNaN(end) {
		return end
	}
	return snap.Ms(o.StartedAt) + o.DurationS.F()*1000
}

// isoMs is a time in ms as Date.prototype.toISOString, nil when NaN.
func isoMs(ms float64) *string {
	if math.IsNaN(ms) {
		return nil
	}
	s := JSISO(time.UnixMilli(int64(math.Round(ms))))
	return &s
}

// incidentFacts is the numbers and codes of an incident made of outages
// (one for profile.incidents[], a product's overlapping ones for a changelog
// incident), made here alone so the three places agree. Its kind and
// detectedBy are its first outage's: a pulse outage is down, a census one a
// stalled census (outages.py).
func incidentFacts(outages []snap.Outage) model.IncidentFacts {
	f := model.IncidentFacts{CountryCodes: []string{}, Kind: "down"}
	start, end, first := math.NaN(), math.NaN(), -1
	for i, o := range outages {
		if !slices.Contains(f.CountryCodes, o.Country) {
			f.CountryCodes = append(f.CountryCodes, o.Country)
		}
		if s := snap.Ms(o.StartedAt); first < 0 || !math.IsNaN(s) && (math.IsNaN(start) || s < start) {
			start, first = s, i
		}
		if o.EndedAt == nil {
			f.Ongoing = true
		} else if e := outageEnd(o); math.IsNaN(end) || e > end {
			end = e
		}
	}
	slices.Sort(f.CountryCodes)
	f.Countries = Num(len(f.CountryCodes))
	if first >= 0 {
		f.DetectedBy = outages[first].Source
	}
	if f.DetectedBy == "census" {
		f.Kind = "census_stall"
	}
	f.StartedAt = isoMs(start)
	if !f.Ongoing {
		f.EndedAt = isoMs(end)
	}
	return f
}
