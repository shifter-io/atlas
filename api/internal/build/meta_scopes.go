package build

import (
	"slices"

	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/Zeus-Development/proxy-index/api/internal/snap"
	"github.com/Zeus-Development/proxy-index/api/internal/store"
)

// The test scopes of /v1/meta and the dataset file (api/answers/meta.md):
// codes and numbers, the website writes the words (#962).

// The production cadence (docs/api-web-map.md section 14): one daily test
// per network and country a day (formulas/<network>.json schedule) and one
// browser run per network and target a day (the browser queue's
// queue_period_minutes = 1440). Neither the snapshot nor the database
// carries them.
const (
	dailyTestsPerCountry       = 1
	browserRunsPerTargetPerDay = 1
)

// The geographic coverage codes of site.scopes.geo_coverage.
const (
	geoAllUsable  = "all_usable_countries"
	geoInventory  = "provider_inventory"
	geoRestricted = "restricted"
)

// restrictedCountries is the size of the restricted (mobile) scope's
// country set: the methodology's 6 countries (api/scripts/catalog-scopes.sql);
// no configuration carries it.
const restrictedCountries = 6

// testScopes is the catalog's scopes in its order. A scope over every target
// runs the dataset's measured targets, in dataset order. Its countries stay
// null until the networks are read (addScopeCountries). No scope has a pulse
// interval: production runs no HTTP pulses (no pulse node, docs/api-web-map.md
// section 14).
func testScopes(rows []store.ScopeRow, targets []model.TargetMeta) []model.TestScope {
	out := make([]model.TestScope, 0, len(rows))
	for _, r := range rows {
		s := model.TestScope{ID: r.Kind, Targets: append([]string{}, r.TargetIDs...), Countries: nan,
			Cadence: model.ScopeCadence{DailyTestsPerCountry: dailyTestsPerCountry,
				BrowserRunsPerTargetPerDay: browserRunsPerTargetPerDay, PulseIntervalMinutes: nan},
			GeoCoverage: scopeGeo(r.GeoCoverage), LiveIPSampling: r.LiveIPSampling, PoolTracking: r.PoolTracking,
			OverlapAudit: r.OverlapAudit, StickinessTest: r.Stickiness, Label: r.Label, Summary: r.Summary}
		if r.AllTargets {
			s.Targets = measuredTargetIDs(targets)
		}
		out = append(out, s)
	}
	return out
}

// measuredTargetIDs is the ids of the measured targets, in order.
func measuredTargetIDs(targets []model.TargetMeta) []string {
	out := []string{}
	for _, t := range targets {
		if t.Source == model.SourceMeasured {
			out = append(out, t.ID)
		}
	}
	return out
}

// scopeGeo is a geo_coverage code with its fixed country set's size (the
// restricted scope's only). A value that is not a code (the sentence a row
// held before #962) leaves both null.
func scopeGeo(code *string) model.ScopeGeo {
	g := model.ScopeGeo{Countries: nan}
	c := model.SV(code)
	if !slices.Contains([]string{geoAllUsable, geoInventory, geoRestricted}, c) {
		return g
	}
	g.Code = model.S(c)
	if c == geoRestricted {
		g.Countries = restrictedCountries
	}
	return g
}

// addScopeCountries counts each scope's tested countries: the distinct
// requested countries (a network answer's requestedCountries) of the
// published product groups of every network whose measured product of a
// kind is the scope's. A scope no published network measures stays null.
func addScopeCountries(scopes []model.TestScope, entries []*entry, groups map[string][]*snap.Product) {
	byProduct := map[string][]*snap.Product{}
	for _, e := range entries {
		for _, kind := range networkRow(e).Kinds {
			product := e.P.MeasuredProduct
			if pm := e.P.ProductMeasurements.Get(kind); pm != nil {
				product = pm.Product
			}
			byProduct[product] = append(byProduct[product], groupsOfKind(e, groups, kind)...)
		}
	}
	for i := range scopes {
		if g := byProduct[scopes[i].ID]; len(g) > 0 {
			scopes[i].Countries = Num(len(requestedCountries(g)))
		}
	}
}
