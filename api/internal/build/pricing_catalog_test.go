package build_test

import (
	"encoding/json"
	"os"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/testdb"
)

func TestShifterPricingCatalog(t *testing.T) {
	t.Parallel()
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

// The VALUE-TEST-1 test catalog (#839) loads into a fresh site schema, twice, and every
// network it prices is served under its website name with its plans and its entry and
// at-scale rates (the same figures as its price evidence).
func TestScoreDataCatalogPrices(t *testing.T) {
	t.Parallel()
	db := testdb.New(t)
	raw, err := os.ReadFile(repoFile(t, "scripts", "scale", "score-data", "price-evidence.json"))
	if err != nil {
		t.Fatal(err)
	}
	var evidence []struct {
		Provider, Metric string
		Value            float64
	}
	if err := json.Unmarshal(raw, &evidence); err != nil {
		t.Fatal(err)
	}
	want := map[string]map[string]float64{}
	for _, e := range evidence {
		if want[e.Provider] == nil {
			want[e.Provider] = map[string]float64{}
			db.Exec(t, "INSERT INTO providers VALUES ($1)", e.Provider)
			db.Exec(t, "INSERT INTO products(provider,name) VALUES ($1,'residential')", e.Provider)
		}
		want[e.Provider][e.Metric] = e.Value
	}
	if len(want) != 16 {
		t.Fatalf("evidence prices %d networks, want 16", len(want))
	}
	sql, err := os.ReadFile(repoFile(t, "scripts", "scale", "score-data", "catalog-prices.sql"))
	if err != nil {
		t.Fatal(err)
	}
	db.Exec(t, string(sql))
	db.Exec(t, string(sql)) // A reload is an upsert, not a second set of plans.
	result := buildFrom(t, db)
	for provider, prices := range want {
		detail := result.Details[provider]
		if detail == nil {
			t.Fatalf("%s missing", provider)
		}
		pricing := detail.Provider.Pricing
		if len(pricing.Plans) == 0 || pricing.Plans[0].Catalog == nil {
			t.Fatalf("%s has no plans", provider)
		}
		if pricing.PricePerGb.F() != prices["price_per_gb_entry"] || pricing.PricePerGbAtScale.F() != prices["price_per_gb_lowest"] {
			t.Fatalf("%s rates %v/%v, evidence %v", provider, pricing.PricePerGb, pricing.PricePerGbAtScale, prices)
		}
	}
	for slug, name := range map[string]string{"iproyal": "IPRoyal", "proxy-cheap": "Proxy Cheap", "nodemaven": "NodeMaven"} {
		if got := result.Details[slug].Provider.Name; got != name {
			t.Fatalf("%s is named %q, want %q", slug, got, name)
		}
	}
	plans := result.Details["iproyal"].Provider.Pricing.Plans
	if len(plans) != 4 || *plans[0].Name != "Entry" || plans[0].Price.F() != 7 || plans[0].Gb.F() != 1 {
		t.Fatalf("IPRoyal plans %+v", plans)
	}
}
