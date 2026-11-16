package build

import (
	"slices"

	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/snap"
	"github.com/Zeus-Development/atlas/api/internal/store"
)

// The test scopes of /v1/meta and the dataset file (api/answers/meta.md):
// codes and numbers, the website writes the words (#966).

// The production cadence (docs/api-web-map.md section 14): one daily test
// per network and country a day (formulas/<network>.json schedule) and 109
// browser tries per network and target a day (the browser queue's [[target]]
// tries = 100 every_minutes = 1320: one run of 100 tries every 22 hours,
// 100 x 1440 / 1320 = 109.09 a day, BROWSER-CYCLE-22H-1). Neither the snapshot
// nor the database carries them.
const (
	dailyTestsPerNetworkCountryPerDay = 1
	browserTriesPerTargetPerDay       = 109
)

// The geographic coverage codes of site.scopes.geo_coverage.
const (
	geoAllUsable  = "all_usable_countries"
	geoInventory  = "provider_inventory"
	geoRestricted = "restricted"
)

// testScopes is the catalog's scopes in its order. A scope over every target
// runs the dataset's measured targets, in dataset order. Its countries and
// pulse interval stay null until the snapshot is read (addScopeCountries,
// addScopePulse).
func testScopes(rows []store.ScopeRow, targets []model.TargetMeta) []model.TestScope {
	out := make([]model.TestScope, 0, len(rows))
	for _, r := range rows {
		s := model.TestScope{ID: r.Kind, Targets: append([]string{}, r.TargetIDs...), Countries: nan,
			Cadence: model.ScopeCadence{DailyTestsPerNetworkCountryPerDay: dailyTestsPerNetworkCountryPerDay,
				BrowserTriesPerTargetPerDay: browserTriesPerTargetPerDay, PulseIntervalMinutes: nan},
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

// scopeGeo is a geo_coverage code; its countries stay null until the
// networks are read (addScopeCountries). A value that is not a code (the
// sentence a row held before #966) leaves the code null (reason catalog).
func scopeGeo(code *string) model.ScopeGeo {
	g := model.ScopeGeo{Countries: nan}
	if c := model.SV(code); slices.Contains([]string{geoAllUsable, geoInventory, geoRestricted}, c) {
		g.Code = model.S(c)
	}
	return g
}

// addScopeCountries counts each scope's tested countries: the distinct
// requested countries (a network answer's requestedCountries) of the
// published product groups of every network whose measured product of a
// kind is the scope's. They are both its countries and its geoCoverage's.
// A scope no published network measures stays null (not_measured).
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
			scopes[i].GeoCoverage.Countries = scopes[i].Countries
		}
	}
}

// addScopePulse sets every scope's pulse interval to the snapshot's
// (pulseMinutes); null without pulse batches (no_pulse_fleet).
func addScopePulse(scopes []model.TestScope, s *snap.Snapshot) {
	minutes := pulseMinutes(s)
	for i := range scopes {
		scopes[i].Cadence.PulseIntervalMinutes = minutes
	}
}

// pulseMinutes is the published snapshot's pulse interval: the scoring
// slots' pulse_interval_minutes of the first product group with a pulse
// batch in a window (uptime.batches). NaN when no group has one: the
// calculator falls back to the targets' configured interval even when no
// pulse ran, so the interval alone does not show a pulse fleet.
func pulseMinutes(s *snap.Snapshot) Num {
	if s == nil {
		return nan
	}
	for _, p := range s.Products {
		for _, w := range p.Windows {
			if w.Uptime != nil && w.Uptime.Batches > 0 && p.Scoring.Slots.PulseIntervalMinutes.OK() {
				return p.Scoring.Slots.PulseIntervalMinutes
			}
		}
	}
	return nan
}

// scopeNA fills a scope's na with a reason each: its countries are
// not_measured without a published network, its pulse interval
// no_pulse_fleet without pulses, every other null (the geo_coverage code,
// the booleans) is the catalog row's.
func scopeNA(s *model.TestScope) error {
	na, err := naOf(*s, nil, false)
	if err != nil {
		return err
	}
	codes := make([]string, len(na))
	for i, p := range na {
		switch p {
		case "countries", "geoCoverage.countries":
			codes[i] = NANotMeasured
		case "cadence.pulseIntervalMinutes":
			codes[i] = NANoPulseFleet
		default:
			codes[i] = NACatalog
		}
	}
	var l NAList
	l.set(na, codes)
	s.NA, s.NAReason, s.NAReasons = l.NA, l.Reason, l.Reasons
	return nil
}
