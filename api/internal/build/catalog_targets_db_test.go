package build_test

import (
	"os"
	"slices"
	"testing"

	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/testdb"
)

// The catalog scripts (api/scripts/catalog-targets.sql, catalog-scopes.sql,
// catalog-profiles.sql), run twice as a refresh does: every target gets its
// category and difficulty while a framework target keeps its own label and
// pass condition, the five scopes come in the website's order, each over every
// measured target with its geographic coverage code and booleans (#962: only
// the countries, the pulse interval and the coverage's country count may be
// null, each with its reason), and the five profiles reach /v1/meta, /v1/home and /v1/rankings.
func TestTargetsScopesAndProfilesCatalog(t *testing.T) {
	t.Parallel()
	db := testdb.New(t)
	scripts := []string{"catalog-targets.sql", "catalog-scopes.sql", "catalog-profiles.sql"}
	for _, name := range append(scripts, scripts...) {
		sql, err := os.ReadFile(repoFile(t, "api", "scripts", name))
		if err != nil {
			t.Fatal(err)
		}
		db.Exec(t, string(sql))
	}
	res := buildFrom(t, db)
	ds := res.Dataset
	if len(ds.Targets) != 12 {
		t.Fatalf("%d targets: %+v", len(ds.Targets), ds.Targets)
	}
	for _, tg := range ds.Targets {
		if model.SV(tg.Category) == "" || !tg.Difficulty.OK() {
			t.Errorf("%s: category %v, difficulty %v", tg.ID, tg.Category, tg.Difficulty)
		}
	}
	http := ds.Targets[0]
	if http.ID != "generic_http" || model.SV(http.Label) != "Generic HTTP" || model.SV(http.Category) != "baseline" ||
		http.Difficulty != 1 || http.Source != model.SourceMeasured || http.PassCondition == nil {
		t.Errorf("generic_http %+v", http)
	}
	measured := []string{}
	for _, tg := range ds.Targets {
		if tg.Source == model.SourceMeasured {
			measured = append(measured, tg.ID)
		}
	}
	nullable := []string{"cadence.pulseIntervalMinutes", "countries", "geoCoverage.countries"}
	ids, codes := []string{}, []string{}
	for _, s := range ds.Scopes {
		ids, codes = append(ids, s.ID), append(codes, model.SV(s.GeoCoverage.Code))
		if !slices.Equal(s.Targets, measured) || s.LiveIPSampling == nil || s.StickinessTest == nil || s.Summary == nil ||
			slices.ContainsFunc(s.NA, func(p string) bool { return !slices.Contains(nullable, p) }) {
			t.Errorf("%s: targets %v, na %v", s.ID, s.Targets, s.NA)
		}
	}
	if want := []string{"all_usable_countries", "all_usable_countries", "provider_inventory", "provider_inventory", "restricted"}; !slices.Equal(codes, want) {
		t.Errorf("geo coverage codes %v", codes)
	}
	// No network is measured and no pulse ran (#994): each null has its reason.
	want := map[string]string{"countries": "not_measured", "geoCoverage.countries": "not_measured",
		"cadence.pulseIntervalMinutes": "no_pulse_fleet"}
	for _, s := range metaOf(t, res).Scopes {
		for _, p := range s.NA {
			if got := s.NAReasons[p]; got == "" && s.NAReason != want[p] || got != "" && got != want[p] {
				t.Errorf("%s: %s reason %q (%v)", s.ID, p, s.NAReason, s.NAReasons)
			}
		}
	}
	if want := []string{"residential_rotating", "residential_sticky", "isp_static", "datacenter", "mobile"}; !slices.Equal(ids, want) {
		t.Errorf("scopes %v", ids)
	}
	if m := metaOf(t, res); len(m.Scopes) != 5 || len(m.Targets) != 12 {
		t.Errorf("meta: %d scopes, %d targets", len(m.Scopes), len(m.Targets))
	}
	profiles := []string{}
	for _, p := range ds.Profiles {
		profiles = append(profiles, p.ID)
	}
	if want := []string{"balanced", "hard-targets", "cost-first", "latency-first", "compliance"}; !slices.Equal(profiles, want) ||
		ds.Profiles[0].Weights.Get("reliability") != 22 || model.SV(ds.Profiles[3].Label) != "Latency first" {
		t.Errorf("profiles %+v", ds.Profiles)
	}
	if m, r := metaOf(t, res), res.Pages.Rankings["30d"]; len(m.Profiles) != 5 || r == nil || len(r.Profiles) != 5 {
		t.Errorf("meta %d or rankings profiles %v", len(m.Profiles), r)
	}
	if h := homeBody(t, res, "24h"); len(h.Profiles) != 5 {
		t.Errorf("home: %d profiles", len(h.Profiles))
	}
}
