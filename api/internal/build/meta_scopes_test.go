package build

import (
	"encoding/json"
	"testing"

	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/snap"
	"github.com/Zeus-Development/atlas/api/internal/store"
)

// scopeNetwork is a linked network whose residential kind measures product.
func scopeNetwork(slug, product string) *entry {
	pm := model.NewMeasurementMap()
	pm.Set("residential", &model.ProductMeasurement{Kind: "residential", Product: product})
	return &entry{P: &model.Provider{Slug: slug, PrimaryKind: "residential", MeasuredProduct: product, ProductMeasurements: pm,
		Framework: &model.FrameworkLink{Provider: slug}}}
}

// scopesMeta is the /v1/meta scopes of two rotating networks (US, DE and
// US, FR), a sticky one (JP) and no mobile one, in a snapshot without pulse
// batches, each scope's na filled as the build does.
func scopesMeta(t *testing.T, rows []store.ScopeRow) []model.TestScope {
	t.Helper()
	targets := []model.TargetMeta{{ID: "generic_http", Source: model.SourceMeasured}, {ID: "amazon_pdp", Source: model.SourceMeasured},
		{ID: "ticketing", Source: "catalog"}}
	ds := &model.Dataset{Weights: model.NewBreakdown(), Targets: targets, Scopes: testScopes(rows, targets)}
	group := func(provider string, countries ...string) snap.Product {
		return snap.Product{Provider: provider, Kind: "residential", Criteria: snap.Criteria{RequestedCountries: countries}}
	}
	s := &snap.Snapshot{Products: []snap.Product{group("rot1", "us", "DE"), group("rot2", "US", "FR"), group("sticky", "JP")}}
	entries := []*entry{scopeNetwork("rot1", "residential_rotating"), scopeNetwork("rot2", "residential_rotating"),
		scopeNetwork("sticky", "residential_sticky")}
	addScopeCountries(ds.Scopes, entries, groupsByProvider(s))
	addScopePulse(ds.Scopes, s)
	for i := range ds.Scopes {
		if err := scopeNA(&ds.Scopes[i]); err != nil {
			t.Fatal(err)
		}
	}
	bodies, err := buildMeta(&Context{GeneratedAt: "now", Dataset: ds})
	if err != nil {
		t.Fatal(err)
	}
	return bodies[metaPath].(MetaAnswer).Scopes
}

// A scope is served as codes and numbers (#962): its measured targets, the
// countries its networks were tested in (its geoCoverage's too), its cadence
// in numbers and its geographic coverage code, without the label, summary,
// rationale or the cadence and coverage sentences, which stay internal or
// unread. Every null has its reason (#994): an unmeasured scope's countries
// not_measured, the pulse interval without pulses no_pulse_fleet, an empty
// catalog boolean catalog.
func TestMetaScopesAreCodesAndNumbers(t *testing.T) {
	t.Parallel()
	yes, no := model.B(true), model.B(false)
	rows := []store.ScopeRow{
		{Kind: "residential_rotating", Label: model.S("Rotating residential"), Summary: model.S("A new exit IP per request."), AllTargets: true,
			LiveIPSampling: yes, PoolTracking: yes, OverlapAudit: yes, Stickiness: no, GeoCoverage: model.S(geoAllUsable)},
		{Kind: "residential_sticky", TargetIDs: []string{"amazon_pdp"}, GeoCoverage: model.S(geoInventory)},
		{Kind: "mobile", AllTargets: true, LiveIPSampling: yes, PoolTracking: yes, OverlapAudit: yes, Stickiness: yes,
			GeoCoverage: model.S(geoRestricted)},
	}
	scopes := scopesMeta(t, rows)
	cadence := `"cadence":{"dailyTestsPerNetworkCountryPerDay":1,"browserTriesPerTargetPerDay":100,"pulseIntervalMinutes":null}`
	want := []string{
		`{"id":"residential_rotating","targets":["generic_http","amazon_pdp"],"countries":3,` + cadence +
			`,"geoCoverage":{"code":"all_usable_countries","countries":3},"liveIpSampling":true,"poolTracking":true,` +
			`"overlapAudit":true,"stickinessTest":false,"na":["cadence.pulseIntervalMinutes"],"naReason":"no_pulse_fleet"}`,
		`{"id":"residential_sticky","targets":["amazon_pdp"],"countries":1,` + cadence +
			`,"geoCoverage":{"code":"provider_inventory","countries":1},"liveIpSampling":null,"poolTracking":null,` +
			`"overlapAudit":null,"stickinessTest":null,"na":["cadence.pulseIntervalMinutes",` +
			`"liveIpSampling","overlapAudit","poolTracking","stickinessTest"],"naReason":"catalog",` +
			`"naReasons":{"cadence.pulseIntervalMinutes":"no_pulse_fleet"}}`,
		`{"id":"mobile","targets":["generic_http","amazon_pdp"],"countries":null,` + cadence +
			`,"geoCoverage":{"code":"restricted","countries":null},"liveIpSampling":true,"poolTracking":true,` +
			`"overlapAudit":true,"stickinessTest":true,"na":["cadence.pulseIntervalMinutes","countries","geoCoverage.countries"],` +
			`"naReason":"not_measured","naReasons":{"cadence.pulseIntervalMinutes":"no_pulse_fleet"}}`,
	}
	if len(scopes) != len(want) {
		t.Fatalf("%d scopes", len(scopes))
	}
	for i, s := range scopes {
		if got := jsonString(t, s); got != want[i] {
			t.Errorf("scope %d:\n got %s\nwant %s", i, got, want[i])
		}
	}
	for _, p := range naWithoutReason(t, scopes) {
		t.Errorf("scopes: %s", p)
	}
	// The label and summary are read, never served.
	if model.SV(scopes[0].Label) != "Rotating residential" || model.SV(scopes[0].Summary) != "A new exit IP per request." {
		t.Errorf("internal label %v, summary %v", scopes[0].Label, scopes[0].Summary)
	}
}

// The marshaled meta scope has none of the removed text fields, and its
// cadence and geoCoverage are objects, not sentences.
func TestMetaScopesServeNoText(t *testing.T) {
	t.Parallel()
	rows := []store.ScopeRow{{Kind: "residential_rotating", Label: model.S("Rotating residential"), Summary: model.S("A sentence."),
		AllTargets: true, GeoCoverage: model.S(geoAllUsable)}}
	var scope map[string]json.RawMessage
	if err := json.Unmarshal([]byte(jsonString(t, scopesMeta(t, rows)[0])), &scope); err != nil {
		t.Fatal(err)
	}
	for _, k := range []string{"label", "summary", "rationale"} {
		if _, ok := scope[k]; ok {
			t.Errorf("served %s: %s", k, scope[k])
		}
	}
	for _, k := range []string{"cadence", "geoCoverage"} {
		if v := scope[k]; len(v) == 0 || v[0] != '{' {
			t.Errorf("%s is not an object: %s", k, v)
		}
	}
}

// A geo_coverage that is not a code (a row's sentence from before #962)
// serves a null code, listed in na with the reason catalog; the restricted
// code has no country count of its own (#994); a scope with no measured
// target runs [].
func TestMetaScopeWithoutACode(t *testing.T) {
	t.Parallel()
	for _, geo := range []string{"All countries with usable coverage", geoRestricted} {
		if g := scopeGeo(model.S(geo)); g.Code != nil && geo != geoRestricted || g.Countries.OK() {
			t.Errorf("%s: geo %+v", geo, g)
		}
	}
	scopes := testScopes([]store.ScopeRow{{Kind: "mobile", AllTargets: true, GeoCoverage: model.S("Restricted to 6 countries")}}, nil)
	if err := scopeNA(&scopes[0]); err != nil {
		t.Fatal(err)
	}
	l := NAList{NA: scopes[0].NA, Reason: scopes[0].NAReason, Reasons: scopes[0].NAReasons}
	if scopes[0].Targets == nil || len(scopes[0].Targets) != 0 || l.ReasonOf("geoCoverage.code") != NACatalog {
		t.Errorf("targets %v, na %+v", scopes[0].Targets, l)
	}
}

// The pulse interval is the snapshot's scoring slots' when a product group
// has a pulse batch in a window, on every scope; a snapshot whose groups
// carry the configured interval but ran no pulse (production's daily tests,
// #841) leaves it null with the reason no_pulse_fleet (#994).
func TestMetaScopePulseIntervalIsTheSnapshots(t *testing.T) {
	t.Parallel()
	product := func(batches Num) snap.Product {
		p := snap.Product{Windows: map[string]snap.Window{"30d": {Uptime: &snap.Uptime{Batches: batches}}}}
		p.Scoring.Slots.PulseIntervalMinutes = 30
		return p
	}
	for _, c := range []struct {
		s    *snap.Snapshot
		want Num
	}{
		{nil, nan},
		{&snap.Snapshot{Products: []snap.Product{{}}}, nan},
		{&snap.Snapshot{Products: []snap.Product{product(0)}}, nan},
		{&snap.Snapshot{Products: []snap.Product{product(0), product(48)}}, 30},
	} {
		scopes := testScopes([]store.ScopeRow{{Kind: "residential_rotating"}, {Kind: "mobile"}}, nil)
		addScopePulse(scopes, c.s)
		for _, s := range scopes {
			if err := scopeNA(&s); err != nil {
				t.Fatal(err)
			}
			got := s.Cadence.PulseIntervalMinutes
			if got.OK() != c.want.OK() || got.OK() && got != c.want ||
				!got.OK() && s.NAReasons["cadence.pulseIntervalMinutes"] != NANoPulseFleet {
				t.Errorf("%s: pulse %v, na %v %s %v", s.ID, got, s.NA, s.NAReason, s.NAReasons)
			}
		}
	}
}

// The snapshot's scoring slots decode their pulse interval; a group
// without one decodes null.
func TestASnapshotGroupsPulseIntervalDecodes(t *testing.T) {
	t.Parallel()
	var with, without snap.Product
	if err := json.Unmarshal([]byte(`{"id":"a","scoring":{"slots":{"minutes":30,"pulse_interval_minutes":30}}}`), &with); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal([]byte(`{"id":"b","scoring":{"slots":{"minutes":5,"pulse_interval_minutes":null}}}`), &without); err != nil {
		t.Fatal(err)
	}
	if with.Scoring.Slots.PulseIntervalMinutes != 30 || without.Scoring.Slots.PulseIntervalMinutes.OK() {
		t.Errorf("pulse %v, %v", with.Scoring.Slots.PulseIntervalMinutes, without.Scoring.Slots.PulseIntervalMinutes)
	}
}
