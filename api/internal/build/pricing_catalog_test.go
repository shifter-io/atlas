package build_test

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/shifter-io/atlas/api/internal/build"
	"github.com/shifter-io/atlas/api/internal/model"
	"github.com/shifter-io/atlas/api/internal/store"
	"github.com/shifter-io/atlas/api/internal/testdb"
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
// fresh site schema, and the network is served with plans, a minimum commit, a trial text, its
// true checks' price history rows (#940) and the same entry and at-scale rates as its
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
	figures := map[string]map[[2]float64]bool{} // #940: a network's true checks show no false change
	for _, r := range loadInputs(t, db).PricingHistory {
		if r.Category == "residential" && r.Source != nil && strings.HasPrefix(*r.Source, "https://") {
			history[r.ProviderSlug]++
			if figures[r.ProviderSlug] == nil {
				figures[r.ProviderSlug] = map[[2]float64]bool{}
			}
			figures[r.ProviderSlug][[2]float64{*r.PricePerGb, *r.PricePerGbAtScale}] = true
		}
	}
	for slug, f := range figures {
		if len(f) != 1 {
			t.Errorf("%s: history rows with different figures %v (a false price change)", slug, f)
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
		if !slices.ContainsFunc(p.Plans, func(pl model.PricingPlan) bool { return pl.Gb.F() == rates["price_basis_plan_gb"] }) {
			t.Errorf("%s: no published plan of the basis's %v GB", slug, rates["price_basis_plan_gb"])
		}
		checks := 1 // the first true check, 2026-11-12 (#940); Shifter also its verified 2026-10-04
		if slug == "shifter" {
			checks = 2
		}
		if len(p.Plans) == 0 || !p.MinCommit.OK() || p.Trial == nil || *p.Trial == "" || history[slug] != checks {
			t.Errorf("%s: %d plans, min commit %v, trial %v, %d history rows", slug, len(p.Plans), p.MinCommit, p.Trial, history[slug])
		}
	}
}

// The #940 reset on proxy_reviews as it was: a network's old-catalog 2026-11-11 row goes and
// Shifter's verified 2026-10-04 check is restated (entry 2.00, at scale 0.75), so after the
// reset and the catalog files the history shows no price change.
func TestPricingHistoryResetLeavesNoFalseChange(t *testing.T) {
	t.Parallel()
	db := testdb.New(t)
	load := func(name string) {
		sql, err := os.ReadFile(repoFile(t, "api", "scripts", name))
		if err != nil {
			t.Fatal(err)
		}
		db.Exec(t, string(sql))
	}
	for _, slug := range []string{"proxyempire", "shifter"} {
		db.Exec(t, "INSERT INTO providers VALUES ($1)", slug)
		db.Exec(t, "INSERT INTO products(provider,name) VALUES ($1,'residential')", slug)
		load("pricing-" + slug + ".sql")
	}
	db.Exec(t, `INSERT INTO site.pricing_history(provider_slug,category,effective_from,price_per_gb,price_per_gb_at_scale,source,is_demo) VALUES
('proxyempire','residential','2026-11-11',3.5,0.75,'https://proxyempire.io/pricing/',false)`)
	db.Exec(t, `UPDATE site.pricing_history SET price_per_gb=1.0 WHERE provider_slug='shifter' AND effective_from='2026-10-04'`)
	load("pricing-history-reset-2026-11-12.sql")
	load("pricing-proxyempire.sql")
	load("pricing-shifter.sql")
	rows := map[string][]string{}
	for _, r := range loadInputs(t, db).PricingHistory {
		rows[r.ProviderSlug] = append(rows[r.ProviderSlug], fmt.Sprintf("%s %v/%v", r.EffectiveFrom.Format("2006-01-02"), *r.PricePerGb, *r.PricePerGbAtScale))
	}
	if got := strings.Join(rows["proxyempire"], ", "); got != "2026-11-12 3.5/1.5" {
		t.Errorf("proxyempire history %s", got)
	}
	if got := strings.Join(rows["shifter"], ", "); got != "2026-10-04 2/0.75, 2026-11-12 2/0.75" {
		t.Errorf("shifter history %s", got)
	}
	held := buildFrom(t, db).Pages.Held["pricing-history"]
	for _, slug := range []string{"proxyempire", "shifter"} {
		ans, _ := held["/v1/pricing-history/"+slug+"/primary"].(*build.PricingHistoryAnswer)
		if ans == nil || ans.LastChangePct.OK() {
			t.Errorf("%s: a price change in %+v", slug, ans)
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
	// COST-BASIS-200GB-1: the cost basis's rate and plan GB too, the rate between the lowest and
	// the entry rate.
	lowest, entry, basis := rates["price_per_gb_lowest"], rates["price_per_gb_entry"], rates["price_per_gb_basis"]
	if len(records) != 4 || len(rates) != 4 || lowest > entry || basis < lowest || basis > entry || !(rates["price_basis_plan_gb"] > 0) {
		t.Fatalf("%s: want a lowest, an entry and a basis rate with its plan GB, lowest <= basis <= entry: %v", file, rates)
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
