package build_test

import (
	"context"
	"encoding/json"
	"math"
	"path/filepath"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"testing"

	"github.com/Zeus-Development/atlas/api/internal/build"
	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/testdb"
)

// The pricing facts as codes and numbers (PRICING-CODES-1): every
// api/scripts/pricing-<slug>.sql writes its trial's numbers as its trial text
// says them, and its plans' audience and pool kinds; the API serves them.

// trialFacts is a trial text's kind and numbers by the issue's reading: no
// trial ("None"), a credit (its amount), else a trial whose price is its
// dollar amount (0 for a free one), with its MB or GB, its days and "no card".
type trialFacts struct {
	kind                    string
	price, credit, gb, days float64 // NaN: none stated
	needsCard               *bool
}

var (
	dollars   = regexp.MustCompile(`\$(\d+(?:\.\d+)?)`)
	traffic   = regexp.MustCompile(`(?i)(\d+(?:\.\d+)?)\s*(mb|gb)\b`)
	trialDays = regexp.MustCompile(`(?i)(\d+)[- ]days?\b`)
)

func readTrial(text string) trialFacts {
	nan := math.NaN()
	t := strings.ToLower(text)
	if t == "none" {
		return trialFacts{kind: "none", price: nan, credit: nan, gb: nan, days: nan}
	}
	f := trialFacts{kind: "free", credit: 0, gb: nan, days: nan}
	if m := dollars.FindStringSubmatch(t); m != nil {
		f.kind, f.price = "paid", atof(m[1])
	}
	if strings.Contains(t, "credit") {
		f.kind, f.price, f.credit = "credit", 0, f.price
	}
	if m := traffic.FindStringSubmatch(t); m != nil {
		f.gb = atof(m[1])
		if m[2] == "mb" {
			f.gb /= 1000
		}
	}
	if m := trialDays.FindStringSubmatch(t); m != nil {
		f.days = atof(m[1])
	}
	if strings.Contains(t, "no card") {
		f.needsCard = new(bool)
	}
	return f
}

func atof(s string) float64 {
	v, _ := strconv.ParseFloat(s, 64)
	return v
}

// isTrialNum is whether a served number is v (both NaN: both null).
func isTrialNum(got model.Num, v float64) bool {
	if math.IsNaN(v) {
		return !got.OK()
	}
	return got.OK() && math.Abs(got.F()-v) < 1e-9
}

// The issue's examples, so readTrial reads the texts as the issue does.
func TestReadTrialAsTheIssue(t *testing.T) {
	t.Parallel()
	nan := math.NaN()
	for text, want := range map[string]trialFacts{
		"$5 trial includes 10GB for 3 days":             {kind: "paid", price: 5, gb: 10, days: 3},
		"Paid trial: $3.50 for 750 MB":                  {kind: "paid", price: 3.5, gb: 0.75, days: nan},
		"$1.97 trial, 150 MB, no expiry":                {kind: "paid", price: 1.97, gb: 0.15, days: nan},
		"3-day free trial (100 MB)":                     {kind: "free", gb: 0.1, days: 3},
		"50MB free trial included":                      {kind: "free", gb: 0.05, days: nan},
		"Free trial (no card needed)":                   {kind: "free", gb: nan, days: nan, needsCard: new(bool)},
		"$5 welcome credit (one-time, on registration)": {kind: "credit", credit: 5, gb: nan, days: nan},
	} {
		got := readTrial(text)
		if got.kind != want.kind || got.price != want.price || got.credit != want.credit || !isTrialNum(model.Num(got.gb), want.gb) ||
			!isTrialNum(model.Num(got.days), want.days) || (got.needsCard == nil) != (want.needsCard == nil) {
			t.Errorf("%q: %+v, want %+v", text, got, want)
		}
	}
	if got := readTrial("None"); got.kind != "none" || !math.IsNaN(got.price) {
		t.Errorf("None: %+v", got)
	}
}

// loadPricingScripts loads every network's pricing script twice (a reload
// is an upsert) and the networks' catalog rows, and builds; it returns the
// networks' slugs.
func loadPricingScripts(t *testing.T, db *testdb.DB) (*build.Result, []string) {
	t.Helper()
	files, err := filepath.Glob(filepath.Join(filepath.Dir(repoFile(t, "configs", "pricing", "shifter-residential.json")), "*-residential.json"))
	if err != nil || len(files) != 18 {
		t.Fatalf("%d evidence files (%v), want 18", len(files), err)
	}
	slugs := make([]string, 0, len(files))
	for _, file := range files {
		slug := strings.TrimSuffix(filepath.Base(file), "-residential.json")
		slugs = append(slugs, slug)
		db.Exec(t, "INSERT INTO providers VALUES ($1)", slug)
		db.Exec(t, "INSERT INTO products(provider,name) VALUES ($1,'residential')", slug)
		sql := sqlFile(t, "api", "scripts", "pricing-"+slug+".sql")
		db.Exec(t, sql)
		db.Exec(t, sql)
	}
	db.Exec(t, sqlFile(t, "api", "scripts", "catalog-networks.sql"))
	res := buildFrom(t, db)
	for _, slug := range slugs {
		if a := res.Pages.Network[slug+"/residential"]; res.Details[slug] == nil || a == nil || a.Profile == nil {
			t.Fatalf("%s: no network or residential answer", slug)
		}
	}
	return res, slugs
}

// Every network's trial numbers are its trial text's (Shifter's row keeps
// none: the website shows its money-back guarantee instead), on the dataset
// provider and its network answer, with trialKind credit for a credit.
func TestEveryNetworkTrialInNumbers(t *testing.T) {
	t.Parallel()
	db := testdb.New(t)
	res, slugs := loadPricingScripts(t, db)
	for _, slug := range slugs {
		p := res.Details[slug].Provider.Pricing
		want := readTrial(strOf(p.Trial))
		if slug == "shifter" {
			want = trialFacts{kind: "free", price: math.NaN(), credit: math.NaN(), gb: math.NaN(), days: math.NaN()}
		}
		if !isTrialNum(p.TrialPrice, want.price) || !isTrialNum(p.TrialCredit, want.credit) || !isTrialNum(p.TrialGb, want.gb) ||
			!isTrialNum(p.TrialDays, want.days) || (p.TrialNeedsCard == nil) != (want.needsCard == nil) ||
			p.TrialNeedsCard != nil && *p.TrialNeedsCard != *want.needsCard {
			t.Errorf("%s: trial %q serves %v %v %v %v %v, want %+v", slug, strOf(p.Trial), p.TrialPrice, p.TrialCredit, p.TrialGb,
				p.TrialDays, p.TrialNeedsCard, want)
		}
		prof := res.Pages.Network[slug+"/residential"].Profile
		if got := strOf(prof.Pricing.TrialKind); got != want.kind || !isTrialNum(prof.Pricing.TrialPrice, want.price) {
			t.Errorf("%s: profile trialKind %q trialPrice %v, want %s", slug, got, prof.Pricing.TrialPrice, want.kind)
		}
		if !p.TrialPrice.OK() && !slices.Contains(res.Details[slug].Provider.NA, "pricing.trialPrice") {
			t.Errorf("%s: a null trialPrice not in na %v", slug, res.Details[slug].Provider.NA)
		}
	}
}

// ownPlanNames are the plan names the networks give, in plan order ("" for
// a null name; PROFILE-TEXT-REMOVE-1: a network not listed has every plan's
// name null, never our description), ownPools the pools they name (every
// other pool is null) and mobilePools the networks whose pool also holds
// mobile exits.
var (
	shifterPlans = "Starter Basic Business Growth Scale Advanced Pro Max"
	ownPlanNames = map[string][]string{"dataimpulse": {"Intro", "Basic", "Advanced"}, "massive": strings.Fields("Starter Builder Scaler Pro"),
		"oxylabs":     strings.Fields("Starter Basic Flexible Enterprise"),
		"proxyempire": append([]string{""}, strings.Fields("Starter Hobby Startup Business Company Corporation Enterprise")...),
		"soax":        strings.Fields("Sandbox Builder Team Scale Enterprise"),
		"shifter":     strings.Fields("Spark Launch Boost Surge " + shifterPlans + " " + shifterPlans + " Starter Basic Business Growth")}
	ownPools    = map[string]string{"evomi": "Premium residential", "rayobyte": "Premium residential"}
	mobilePools = map[string]bool{"nodemaven": true, "soax": true}
)

// Every plan serves its audience (new_customers for a plan for new customers
// only) and its pool's kinds, its catalog without the size and period words,
// the network's own plan and pool names or null, and its N/A paths name it by
// its index.
func TestEveryPlanAudienceAndPoolKinds(t *testing.T) {
	t.Parallel()
	db := testdb.New(t)
	res, slugs := loadPricingScripts(t, db)
	byIndex := regexp.MustCompile(`^pricing\.plans\[\d+\]\.`)
	for _, slug := range slugs {
		names := []string{}
		for i, pl := range res.Details[slug].Provider.Pricing.Plans {
			var c map[string]any
			if err := json.Unmarshal(pl.Catalog, &c); err != nil {
				t.Fatal(err)
			}
			kinds := []string{}
			for _, k := range c["poolKinds"].([]any) {
				kinds = append(kinds, k.(string))
			}
			wantKinds := []string{"residential"}
			if mobilePools[slug] {
				wantKinds = append(wantKinds, "mobile")
			}
			audience := "all"
			if slug == "dataimpulse" && i == 0 {
				audience = "new_customers"
			}
			if strOf(pl.Audience) != audience || !slices.Equal(kinds, wantKinds) || c["bandwidth"] != nil || c["period"] != nil || c["audience"] != nil {
				t.Errorf("%s plan %d %q: audience %v, catalog %v", slug, i, strOf(pl.Name), strOf(pl.Audience), c)
			}
			if pool, want := c["pool"], ownPools[slug]; slug != "shifter" && (want == "" && pool != nil || want != "" && pool != want) {
				t.Errorf("%s plan %d: pool %v, want %q (null for none)", slug, i, pool, want)
			}
			names = append(names, strOf(pl.Name))
		}
		want, own := ownPlanNames[slug]
		if !own {
			want = make([]string, len(names))
		}
		if !slices.Equal(names, want) {
			t.Errorf("%s plan names %q, want %q", slug, names, want)
		}
		prof := res.Pages.Network[slug+"/residential"].Profile
		for _, p := range prof.NA {
			if strings.HasPrefix(p, "pricing.plans[") && !byIndex.MatchString(p) {
				t.Errorf("%s: na path %s does not name its plan by index", slug, p)
			}
		}
		unnamedPlansListed(t, slug, prof.NAList, prof.Pricing.Plans)
	}
	dataimpulse := res.Details["dataimpulse"].Provider.Pricing.Plans[0]
	if strOf(dataimpulse.Audience) != "new_customers" {
		t.Errorf("dataimpulse intro plan %q audience %v", strOf(dataimpulse.Name), strOf(dataimpulse.Audience))
	}
}

// unnamedPlansListed checks that a network answer lists exactly its plans'
// null names and pools, each with not_named_by_provider (#1060).
func unnamedPlansListed(t *testing.T, slug string, l build.NAList, plans []model.PricingPlan) {
	t.Helper()
	for i, pl := range plans {
		var c map[string]any
		if err := json.Unmarshal(pl.Catalog, &c); err != nil {
			t.Fatal(err)
		}
		at := "pricing.plans[" + strconv.Itoa(i) + "]."
		for p, null := range map[string]bool{at + "name": pl.Name == nil, at + "catalog.pool": c["pool"] == nil} {
			if listed := slices.Contains(l.NA, p); listed != null || null && l.ReasonOf(p) != build.NANotNamedByProvider {
				t.Errorf("%s: %s null %v, listed %v with %q", slug, p, null, listed, l.ReasonOf(p))
			}
		}
	}
}

// Migration 0005 takes a trial's numbers and refuses a negative price or
// credit and a zero GB or day count.
func TestTrialMigrationChecks(t *testing.T) {
	t.Parallel()
	db := testdb.New(t)
	db.Exec(t, `INSERT INTO site.providers(slug,name) VALUES ('acme','Acme');
	INSERT INTO site.pricing(provider_slug,category,trial,trial_price,trial_credit,trial_gb,trial_days,trial_needs_card)
	  VALUES ('acme','residential','$5 for 10 GB, 3 days',5,0,10,3,true)`)
	c := db.Conn(t)
	defer c.Close(context.Background())
	for _, bad := range []string{
		`UPDATE site.pricing SET trial_price = -1`, `UPDATE site.pricing SET trial_credit = -5`,
		`UPDATE site.pricing SET trial_gb = 0`, `UPDATE site.pricing SET trial_days = 0`,
	} {
		if _, err := c.Exec(context.Background(), bad); err == nil || !strings.Contains(err.Error(), "check constraint") {
			t.Errorf("%s: %v, want a check violation", bad, err)
		}
	}
}
