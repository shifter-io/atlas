package build

import (
	"encoding/json"
	"os"
	"path/filepath"
	"slices"
	"testing"

	"github.com/BurntSushi/toml"

	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/Zeus-Development/proxy-index/api/internal/snap"
	"github.com/Zeus-Development/proxy-index/api/internal/store"
)

// scopeKeys is a served scope's keys: codes, numbers and booleans, no text
// (META-SCOPES-CODES-1, #966).
var scopeKeys = []string{"cadence", "countries", "geoCoverage", "id", "liveIpSampling", "na", "overlapAudit", "poolTracking",
	"stickinessTest", "targets"}

// scopeProduct is a measured snapshot product requesting countries, with a
// 24h time slot of minutes (none when 0).
func scopeProduct(minutes float64, country *string, countries ...string) *snap.Product {
	m := snap.Metrics{}
	if minutes > 0 {
		m.TimeSlots = &snap.TimeSlots{Minutes: model.Num(minutes)}
	}
	return &snap.Product{Criteria: snap.Criteria{Country: country, RequestedCountries: countries},
		Windows: map[string]snap.Window{"24h": {Metrics: m}}}
}

// scopeBuilder is a build of the targets, scopes and networks given.
func scopeBuilder(targets []model.TargetMeta, scopes []store.ScopeRow, entries ...*entry) *builder {
	return &builder{in: &store.Inputs{Scopes: scopes}, ds: &model.Dataset{Targets: targets}, entries: entries}
}

// A tested scope's countries are its networks' requested countries, once each;
// its cadence the configuration's daily tests and browser tries and the
// shortest 24h time slot; its targets the measured ones that are not groups.
// A scope no network is tested in has only nulls, in its na.
func TestScopesInCodesAndNumbers(t *testing.T) {
	de := "de"
	targets := []model.TargetMeta{{ID: "generic_http", Source: model.SourceMeasured, Engine: model.S(model.EngineHTTP)},
		{ID: "amazon_pdp", Source: model.SourceMeasured, Engine: model.S(model.EngineBrowser)},
		{ID: "ai_answers", Source: model.SourceMeasured, Engine: model.S(model.EngineBrowser), Sources: []string{"ai_overview"}},
		{ID: "catalog_only", Source: "catalog"}}
	geo, yes := "all_usable_countries", true
	rows := []store.ScopeRow{{Kind: "residential_rotating", AllTargets: true, GeoCoverage: &geo, LiveIPSampling: &yes},
		{Kind: "mobile", TargetIDs: []string{"generic_http"}}}
	sticky := &model.Provider{MeasuredProduct: "residential_rotating", ProductMeasurements: model.NewMeasurementMap()}
	sticky.ProductMeasurements.Set("residential", &model.ProductMeasurement{Product: "residential_sticky"})
	b := scopeBuilder(targets, rows,
		&entry{P: &model.Provider{MeasuredProduct: "residential_rotating"}, Snap: map[string]*snap.Product{"residential": scopeProduct(30, nil, "US", "DE")}},
		&entry{P: &model.Provider{MeasuredProduct: "residential_rotating"}, Snap: map[string]*snap.Product{"residential": scopeProduct(60, &de, "FR")}},
		&entry{P: sticky, Snap: map[string]*snap.Product{"residential": scopeProduct(5, nil, "JP")}})
	if err := b.scopes(); err != nil {
		t.Fatal(err)
	}
	for i := range b.ds.Scopes {
		var err error
		if b.ds.Scopes[i].NA, err = naOf(b.ds.Scopes[i], nil, false); err != nil {
			t.Fatal(err)
		}
	}
	rot, mobile := b.ds.Scopes[0], b.ds.Scopes[1]
	want := model.ScopeCadence{DailyTestsPerNetworkCountryPerDay: 1, BrowserTriesPerTargetPerDay: 100, PulseIntervalMinutes: 30}
	if rot.ID != "residential_rotating" || !slices.Equal(rot.Targets, []string{"generic_http", "amazon_pdp"}) || rot.Countries != 3 ||
		rot.Cadence != want || model.SV(rot.GeoCoverage) != geo || rot.LiveIPSampling == nil || !*rot.LiveIPSampling {
		t.Errorf("rotating %+v", rot)
	}
	if slices.Contains(rot.NA, "countries") || !slices.Contains(rot.NA, "poolTracking") {
		t.Errorf("rotating na %v", rot.NA)
	}
	if !slices.Equal(mobile.Targets, []string{"generic_http"}) || mobile.Countries.OK() || mobile.Cadence.PulseIntervalMinutes.OK() ||
		mobile.Cadence.DailyTestsPerNetworkCountryPerDay.OK() || mobile.Cadence.BrowserTriesPerTargetPerDay.OK() {
		t.Errorf("mobile %+v", mobile)
	}
	for _, p := range []string{"countries", "cadence.dailyTestsPerNetworkCountryPerDay", "cadence.browserTriesPerTargetPerDay",
		"cadence.pulseIntervalMinutes", "geoCoverage"} {
		if !slices.Contains(mobile.NA, p) {
			t.Errorf("mobile na %v lacks %s", mobile.NA, p)
		}
	}
}

// A tested scope without a browser target has no browser tries, and one
// whose snapshot publishes no time slot no pulse interval.
func TestScopeWithoutBrowserTargetsOrSlots(t *testing.T) {
	b := scopeBuilder([]model.TargetMeta{{ID: "generic_http", Source: model.SourceMeasured, Engine: model.S(model.EngineHTTP)}},
		[]store.ScopeRow{{Kind: "residential_rotating", AllTargets: true}},
		&entry{P: &model.Provider{MeasuredProduct: "residential_rotating"}, Snap: map[string]*snap.Product{"residential": scopeProduct(0, nil)}})
	if err := b.scopes(); err != nil {
		t.Fatal(err)
	}
	s := b.ds.Scopes[0]
	if s.Cadence.DailyTestsPerNetworkCountryPerDay != 1 || s.Cadence.BrowserTriesPerTargetPerDay.OK() || s.Cadence.PulseIntervalMinutes.OK() || s.Countries.OK() {
		t.Errorf("scope %+v", s)
	}
}

// A served scope has exactly the code, number and boolean keys: no label,
// summary, rationale or text cadence; its cadence is three numbers.
func TestScopeServesNoText(t *testing.T) {
	ten := model.Num(10)
	body, err := json.Marshal(model.TestScope{ID: "datacenter", Targets: []string{}, Countries: ten,
		Cadence: model.ScopeCadence{DailyTestsPerNetworkCountryPerDay: 1, BrowserTriesPerTargetPerDay: 100, PulseIntervalMinutes: nan}})
	if err != nil {
		t.Fatal(err)
	}
	var doc map[string]json.RawMessage
	if err := json.Unmarshal(body, &doc); err != nil {
		t.Fatal(err)
	}
	keys := []string{}
	for k := range doc {
		keys = append(keys, k)
	}
	slices.Sort(keys)
	if !slices.Equal(keys, scopeKeys) {
		t.Errorf("keys %v, want %v", keys, scopeKeys)
	}
	if got := string(doc["cadence"]); got != `{"dailyTestsPerNetworkCountryPerDay":1,"browserTriesPerTargetPerDay":100,"pulseIntervalMinutes":null}` {
		t.Errorf("cadence %s", got)
	}
}

// The cadence constants are the test configuration's: the browser queue
// tries every target 100 times per network a UTC day, and every network's
// formula schedules each country at one hour a day.
func TestScopeCadenceMatchesTheConfiguration(t *testing.T) {
	root := filepath.Join("..", "..", "..")
	var queue struct {
		Target []struct {
			ID           string `toml:"id"`
			Tries        int    `toml:"tries"`
			EveryMinutes int    `toml:"every_minutes"`
		} `toml:"target"`
	}
	if _, err := toml.DecodeFile(filepath.Join(root, "configs", "browser-pulse.production.example.toml"), &queue); err != nil {
		t.Fatal(err)
	}
	if len(queue.Target) == 0 {
		t.Fatal("no browser queue target")
	}
	for _, tg := range queue.Target {
		if tg.EveryMinutes <= 0 || tg.Tries*1440/tg.EveryMinutes != browserTriesPerTargetPerDay {
			t.Errorf("%s: %d tries every %d minutes, want %d a day", tg.ID, tg.Tries, tg.EveryMinutes, browserTriesPerTargetPerDay)
		}
	}
	files, err := filepath.Glob(filepath.Join(root, "formulas", "*.json"))
	if err != nil || len(files) == 0 {
		t.Fatalf("no formula (%v)", err)
	}
	for _, f := range files {
		body, err := os.ReadFile(f)
		if err != nil {
			t.Fatal(err)
		}
		// Each country's one test hour, never a list: one daily test.
		var formula struct {
			Schedule map[string]int `json:"schedule"`
		}
		if err := json.Unmarshal(body, &formula); err != nil || len(formula.Schedule) == 0 {
			t.Errorf("%s: schedule %v (%v)", filepath.Base(f), formula.Schedule, err)
		}
	}
	if dailyTestsPerNetworkCountryPerDay != 1 {
		t.Errorf("%d daily tests, the formulas schedule one", dailyTestsPerNetworkCountryPerDay)
	}
}
