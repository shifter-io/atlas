package build_test

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/store"
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

// Every network's list prices (#839): its api/scripts/pricing-<slug>.sql loads twice into a
// fresh site schema, and the network is served with plans, a minimum commit, a trial text, two
// price history rows (the previous check and #927's) and the same entry and at-scale rates as its
// configs/pricing/<slug>-residential.json evidence.
func TestNetworkPricingCatalogs(t *testing.T) {
	t.Parallel()
	db := testdb.New(t)
	files, err := filepath.Glob(filepath.Join(filepath.Dir(repoFile(t, "configs", "pricing", "shifter-residential.json")), "*-residential.json"))
	if err != nil {
		t.Fatal(err)
	}
	if len(files) != 18 {
		t.Fatalf("%d evidence files, want one per network (18)", len(files))
	}
	want := map[string]map[string]float64{}
	for _, file := range files {
		slug := strings.TrimSuffix(filepath.Base(file), "-residential.json")
		want[slug] = evidenceRates(t, file, slug)
		db.Exec(t, "INSERT INTO providers VALUES ($1)", slug)
		db.Exec(t, "INSERT INTO products(provider,name) VALUES ($1,'residential')", slug)
		sql, err := os.ReadFile(repoFile(t, "api", "scripts", "pricing-"+slug+".sql"))
		if err != nil {
			t.Fatal(err)
		}
		db.Exec(t, string(sql))
		db.Exec(t, string(sql)) // A reload is an upsert, not a second set of rows.
	}
	result := buildFrom(t, db)
	history := map[string]int{}
	for _, r := range loadInputs(t, db).PricingHistory {
		if r.Category == "residential" && r.Source != nil && strings.HasPrefix(*r.Source, "https://") {
			history[r.ProviderSlug]++
		}
	}
	for slug, rates := range want {
		detail := result.Details[slug]
		if detail == nil {
			t.Fatalf("%s missing", slug)
		}
		p := detail.Provider.Pricing
		if p.PricePerGb.F() != rates["price_per_gb_entry"] || p.PricePerGbAtScale.F() != rates["price_per_gb_lowest"] {
			t.Errorf("%s serves %v/%v, its evidence says %v", slug, p.PricePerGb, p.PricePerGbAtScale, rates)
		}
		if len(p.Plans) == 0 || !p.MinCommit.OK() || p.Trial == nil || *p.Trial == "" || history[slug] != 2 {
			t.Errorf("%s: %d plans, min commit %v, trial %v, %d history rows", slug, len(p.Plans), p.MinCommit, p.Trial, history[slug])
		}
	}
}

// evidenceRates reads a configs/pricing evidence file: the provider's two list rates.
func evidenceRates(t *testing.T, file, slug string) map[string]float64 {
	t.Helper()
	raw, err := os.ReadFile(file)
	if err != nil {
		t.Fatal(err)
	}
	var records []struct {
		Provider, Product, Cohort, Metric string
		Value                             float64
	}
	if err := json.Unmarshal(raw, &records); err != nil {
		t.Fatal(err)
	}
	rates := map[string]float64{}
	for _, r := range records {
		if r.Provider != slug || r.Product != "residential" || r.Cohort != "*" {
			t.Fatalf("%s: record for %s/%s cohort %s", file, r.Provider, r.Product, r.Cohort)
		}
		rates[r.Metric] = r.Value
	}
	if len(records) != 2 || len(rates) != 2 || rates["price_per_gb_lowest"] > rates["price_per_gb_entry"] {
		t.Fatalf("%s: want a lowest and an entry rate, lowest at most entry: %v", file, rates)
	}
	return rates
}

func loadInputs(t *testing.T, db *testdb.DB) *store.Inputs {
	t.Helper()
	ctx := context.Background()
	pool, err := store.OpenReadOnly(ctx, db.DSN)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	in, err := store.Load(ctx, pool)
	if err != nil {
		t.Fatal(err)
	}
	return in
}
