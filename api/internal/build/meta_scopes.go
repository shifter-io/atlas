package build

import (
	"slices"
	"strings"

	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/Zeus-Development/proxy-index/api/internal/snap"
)

// The test scopes of /v1/meta and the dataset's scopes[] in codes and numbers
// (META-SCOPES-CODES-1, #966): site.scopes gives the codes and booleans
// (api/scripts/catalog-scopes.sql), the snapshot and the test configuration
// the numbers. The website writes the words.

// The cadence the test configuration sets for every network of a tested scope.
const (
	// dailyTestsPerNetworkCountryPerDay: the daily planner tests each network
	// in each country once a UTC day, at the one hour its formula schedules
	// (formulas/*.json schedule; TestScopeCadenceMatchesTheConfiguration).
	dailyTestsPerNetworkCountryPerDay = 1
	// browserTriesPerTargetPerDay: the browser queue's tries of a target per
	// network, once a UTC day (configs/browser-pulse.production.example.toml
	// [[target]] tries and every_minutes).
	browserTriesPerTargetPerDay = 100
)

// scopes fills the dataset's scopes from site.scopes: every target is the
// measured targets a network is tested on (a group target is computed from
// its sources, so it is not one); countries and cadence come from the
// scope's measured networks, NaN for a scope no network is tested in.
func (b *builder) scopes() error {
	ds := b.ds
	ds.Scopes = []model.TestScope{}
	for _, r := range b.in.Scopes {
		targets := append([]string{}, r.TargetIDs...)
		if r.AllTargets {
			targets = testedTargets(ds.Targets)
		}
		s := model.TestScope{ID: r.Kind, Targets: targets, GeoCoverage: r.GeoCoverage, LiveIPSampling: r.LiveIPSampling,
			PoolTracking: r.PoolTracking, OverlapAudit: r.OverlapAudit, StickinessTest: r.Stickiness}
		s.Countries, s.Cadence = scopeFacts(scopeProducts(b.entries, r.Kind))
		if !slices.ContainsFunc(ds.Targets, func(t model.TargetMeta) bool {
			return model.SV(t.Engine) == model.EngineBrowser && slices.Contains(targets, t.ID)
		}) {
			s.Cadence.BrowserTriesPerTargetPerDay = nan
		}
		ds.Scopes = append(ds.Scopes, s)
	}
	return nil
}

// testedTargets is the ids of the measured targets that are not groups, in
// dataset order.
func testedTargets(targets []model.TargetMeta) []string {
	out := []string{}
	for _, t := range targets {
		if t.Source == model.SourceMeasured && len(t.Sources) == 0 {
			out = append(out, t.ID)
		}
	}
	return out
}

// scopeProducts is the snapshot products measured as the product kind: each
// network category's product whose measured product (as protocol reads it)
// is kind.
func scopeProducts(entries []*entry, kind string) []*snap.Product {
	out := []*snap.Product{}
	for _, e := range entries {
		for k, sp := range e.Snap {
			product := e.P.MeasuredProduct
			if pm := e.P.ProductMeasurements.Get(k); pm != nil {
				product = pm.Product
			}
			if sp != nil && product == kind {
				out = append(out, sp)
			}
		}
	}
	return out
}

// scopeFacts is how many countries the products are tested in (their
// requested countries) and their cadence: the configuration's daily tests and
// browser tries, and the pulse interval, the shortest 24h time slot the
// snapshot publishes (the shortest pulse interval of configs/targets.toml,
// methodology.toml [slots]); NaN without one. scopes clears the browser tries
// of a scope without a browser target.
func scopeFacts(products []*snap.Product) (model.Num, model.ScopeCadence) {
	cadence := model.ScopeCadence{DailyTestsPerNetworkCountryPerDay: nan, BrowserTriesPerTargetPerDay: nan, PulseIntervalMinutes: nan}
	if len(products) == 0 {
		return nan, cadence
	}
	cadence.DailyTestsPerNetworkCountryPerDay = dailyTestsPerNetworkCountryPerDay
	cadence.BrowserTriesPerTargetPerDay = browserTriesPerTargetPerDay
	countries := []string{}
	for _, p := range products {
		requested := slices.Clone(p.Criteria.RequestedCountries)
		if c := p.Criteria.Country; c != nil {
			requested = append(requested, strings.ToUpper(*c))
		}
		for _, cc := range requested {
			if !slices.Contains(countries, cc) {
				countries = append(countries, cc)
			}
		}
		ts := p.Window("24h").Metrics.TimeSlots
		if ts != nil && ts.Minutes.OK() && ts.Minutes > 0 && (!cadence.PulseIntervalMinutes.OK() || ts.Minutes < cadence.PulseIntervalMinutes) {
			cadence.PulseIntervalMinutes = ts.Minutes
		}
	}
	n := nan
	if len(countries) > 0 {
		n = model.Num(len(countries))
	}
	return n, cadence
}
