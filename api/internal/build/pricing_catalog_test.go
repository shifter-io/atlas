package build_test

import (
	"encoding/json"
	"os"
	"testing"

	"github.com/Zeus-Development/proxy-index-website/api/internal/testdb"
)

func TestShifterPricingCatalog(t *testing.T) {
	db := testdb.New(t)
	db.Exec(t, `INSERT INTO site.providers(slug,name,kinds,primary_kind) VALUES ('shifter','Shifter','{residential}','residential')`)
	sql, err := os.ReadFile(repoFile(t, "api", "scripts", "pricing-shifter.sql"))
	if err != nil {
		t.Fatal(err)
	}
	db.Exec(t, string(sql))
	db.Exec(t, string(sql)) // A refresh is an upsert, not a second set of plans.
	result := buildFrom(t, db)
	provider := result.Details["shifter"]
	if provider == nil {
		t.Fatal("Shifter missing")
	}
	plans := provider.Provider.Pricing.Plans
	if len(plans) != 24 {
		t.Fatalf("got %d plans, want 24", len(plans))
	}
	counts := map[string]int{}
	for _, plan := range plans {
		var catalog struct {
			Pool     string  `json:"pool"`
			Tier     *string `json:"tier"`
			UnitRate float64 `json:"unitRate"`
			URL      string  `json:"url"`
		}
		if err := json.Unmarshal(plan.Catalog, &catalog); err != nil {
			t.Fatal(err)
		}
		if catalog.UnitRate <= 0 || catalog.URL == "" {
			t.Fatal("offer missing rate or order URL")
		}
		tier := ""
		if catalog.Tier != nil {
			tier = *catalog.Tier
		}
		counts[catalog.Pool+"/"+tier]++
	}
	for _, key := range []string{"Full Geo/Lite", "Full Geo/Regular", "Full Geo/Enterprise", "Country Geo/Regular", "Country Geo/Enterprise", "Non-Geo/"} {
		if counts[key] != 4 {
			t.Fatalf("%s has %d plans", key, counts[key])
		}
	}
	for _, path := range provider.Provider.NA {
		if path == "pricing.plans[].catalog.tier" {
			t.Fatal("untiered offer incorrectly marked unavailable")
		}
	}
}
