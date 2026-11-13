package build

import (
	"encoding/json"
	"slices"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/Zeus-Development/proxy-index/api/internal/snap"
	"github.com/Zeus-Development/proxy-index/api/internal/store"
)

// scopeNetwork is a linked network whose residential kind measures product.
func scopeNetwork(slug, product string) *entry {
	pm := model.NewMeasurementMap()
	pm.Set("residential", &model.ProductMeasurement{Kind: "residential", Product: product})
	return &entry{P: &model.Provider{Slug: slug, PrimaryKind: "residential", MeasuredProduct: product, ProductMeasurements: pm,
		Framework: &model.FrameworkLink{Provider: slug}}}
}

// scopesMeta is the /v1/meta scopes of two rotating networks (US, DE and
// US, FR), a sticky one (JP) and no mobile one, each scope's na filled as
// the build does.
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
	for i := range ds.Scopes {
		var err error
		if ds.Scopes[i].NA, err = naOf(ds.Scopes[i], nil, false); err != nil {
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
// countries its networks were tested in, its cadence in numbers and its
// geographic coverage code, without the label, summary, rationale or the
// cadence and coverage sentences, which stay internal or unread.
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
			`,"geoCoverage":{"code":"all_usable_countries","countries":null},"liveIpSampling":true,"poolTracking":true,` +
			`"overlapAudit":true,"stickinessTest":false,"na":["cadence.pulseIntervalMinutes","geoCoverage.countries"]}`,
		`{"id":"residential_sticky","targets":["amazon_pdp"],"countries":1,` + cadence +
			`,"geoCoverage":{"code":"provider_inventory","countries":null},"liveIpSampling":null,"poolTracking":null,` +
			`"overlapAudit":null,"stickinessTest":null,"na":["cadence.pulseIntervalMinutes","geoCoverage.countries",` +
			`"liveIpSampling","overlapAudit","poolTracking","stickinessTest"]}`,
		`{"id":"mobile","targets":["generic_http","amazon_pdp"],"countries":null,` + cadence +
			`,"geoCoverage":{"code":"restricted","countries":6},"liveIpSampling":true,"poolTracking":true,` +
			`"overlapAudit":true,"stickinessTest":true,"na":["cadence.pulseIntervalMinutes","countries"]}`,
	}
	if len(scopes) != len(want) {
		t.Fatalf("%d scopes", len(scopes))
	}
	for i, s := range scopes {
		if got := jsonString(t, s); got != want[i] {
			t.Errorf("scope %d:\n got %s\nwant %s", i, got, want[i])
		}
	}
	// The label and summary stay internal: profile.protocol reads them.
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
// serves a null code, listed in na; a scope with no measured target runs [].
func TestMetaScopeWithoutACode(t *testing.T) {
	t.Parallel()
	g := scopeGeo(model.S("All countries with usable coverage"))
	if g.Code != nil || g.Countries.OK() {
		t.Errorf("geo %+v", g)
	}
	scopes := testScopes([]store.ScopeRow{{Kind: "mobile", AllTargets: true, GeoCoverage: model.S("Restricted to 6 countries")}}, nil)
	na, err := naOf(scopes[0], nil, false)
	if err != nil {
		t.Fatal(err)
	}
	if scopes[0].Targets == nil || len(scopes[0].Targets) != 0 || !slices.Contains(na, "geoCoverage.code") {
		t.Errorf("targets %v, na %v", scopes[0].Targets, na)
	}
}
