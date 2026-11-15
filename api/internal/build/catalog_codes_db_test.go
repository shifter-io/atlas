package build_test

import (
	"context"
	"slices"
	"strings"
	"testing"

	"github.com/Zeus-Development/atlas/api/internal/build"
	"github.com/Zeus-Development/atlas/api/internal/testdb"
)

// The provider facts as codes (PROVIDER-CODES-1): catalog-network-facts.sql
// writes hq_country, hq_city and use_cases from hq and best_for by these
// tables, and the API serves them (and no longer reads hq and best_for,
// PROFILE-TEXT-REMOVE-1).

// useCaseOf is the issue's mapping of a best_for phrase (lower case) to its
// use_case codes.
var useCaseOf = map[string][]string{
	"price monitoring": {"price_monitoring"}, "price comparison": {"price_monitoring"},
	"price and assortment monitoring": {"price_monitoring"},
	"ad verification":                 {"ad_verification"}, "verify ad placements globally": {"ad_verification"},
	"ad verification and fraud detection": {"ad_verification"}, "ad analytics and creative testing": {"ad_verification"},
	"web scraping": {"web_scraping"}, "scale web scraping": {"web_scraping"}, "data collection": {"web_scraping"},
	"web data collection": {"web_scraping"}, "web intelligence": {"web_scraping"},
	"seo monitoring": {"seo_monitoring"}, "seo tracking": {"seo_monitoring"},
	"search engine optimization": {"seo_monitoring"}, "seo": {"seo_monitoring"},
	"serp tracking": {"serp_tracking"}, "search engine monitoring": {"serp_tracking"},
	"brand protection": {"brand_protection"}, "review monitoring": {"review_monitoring"},
	"market intelligence": {"market_research"}, "competitive analysis": {"market_research"},
	"market research & price monitoring":            {"market_research", "price_monitoring"},
	"collect diverse training data for ai and llms": {"ai_training_data"}, "ai training": {"ai_training_data"},
	"ai training and rag": {"ai_training_data"}, "ai agents": {"ai_agents"},
	"travel fare aggregation": {"travel_fares"}, "social media management": {"social_media"},
	"multi-account management": {"multi_accounting"}, "cybersecurity": {"cybersecurity"},
	"localized content testing": {"localized_testing"}, "geo-targeting": {"localized_testing"},
	"uptime and performance tracking": {"uptime_monitoring"}, "anonymous browsing": {"anonymous_browsing"},
}

// countryOf is the ISO code of each country an hq names; notCities are the
// first parts of an hq that name a state, not a city; cityStates are the hqs
// that name both the country and its city (Shifter's Hong Kong, #1175).
var (
	countryOf = map[string]string{"United Kingdom": "GB", "United States": "US", "Lithuania": "LT", "Switzerland": "CH",
		"United Arab Emirates": "AE", "Singapore": "SG", "Cyprus": "CY", "Bulgaria": "BG", "Hong Kong": "HK"}
	notCities  = map[string]bool{"California": true}
	cityStates = map[string]bool{"Hong Kong": true}
)

// hqCodes is hq as its country code and city ("" for none).
func hqCodes(t *testing.T, hq string) (country, city string) {
	t.Helper()
	city, name, ok := strings.Cut(hq, ", ")
	if !ok {
		city, name = "", hq
		if cityStates[hq] {
			city = hq
		}
	}
	if notCities[city] {
		city = ""
	}
	if countryOf[name] == "" {
		t.Errorf("hq %q names a country without its code", hq)
	}
	return countryOf[name], city
}

// useCasesOf is a network's phrases as codes, in order, a code once.
func useCasesOf(t *testing.T, bestFor []string) []string {
	t.Helper()
	out := []string{}
	for _, phrase := range bestFor {
		codes, ok := useCaseOf[strings.ToLower(phrase)]
		if !ok {
			t.Errorf("best_for %q has no use case", phrase)
		}
		for _, c := range codes {
			if !slices.Contains(out, c) {
				out = append(out, c)
			}
		}
	}
	return out
}

// Every network of the facts script has hq_country when it has hq, and its
// codes are its hq and best_for by the mapping; the API serves them on the
// dataset's providers and each network's profile.
func TestCatalogProviderCodes(t *testing.T) {
	t.Parallel()
	db := testdb.New(t)
	for id := range networkNames {
		db.Exec(t, "INSERT INTO providers VALUES ($1) ON CONFLICT DO NOTHING", id)
	}
	db.Exec(t, sqlFile(t, "api", "scripts", "catalog-networks.sql"))
	facts := sqlFile(t, "api", "scripts", "catalog-network-facts.sql")
	db.Exec(t, facts)
	db.Exec(t, facts)
	text := catalogText(t, db)
	res := buildFrom(t, db)
	for id := range networkNames {
		d := res.Details[id]
		if d == nil {
			t.Fatalf("no network %s", id)
		}
		p, hq := d.Provider, text[id].hq
		country, city := "", ""
		if hq != nil {
			country, city = hqCodes(t, *hq)
		}
		if got := strOf(p.HQCountry); got != country {
			t.Errorf("%s: hqCountry %q, want %q (hq %v)", id, got, country, strOf(hq))
		}
		if got := strOf(p.HQCity); got != city {
			t.Errorf("%s: hqCity %q, want %q (hq %v)", id, got, city, strOf(hq))
		}
		if want := useCasesOf(t, text[id].bestFor); !slices.Equal(p.UseCases, want) || len(want) == 0 {
			t.Errorf("%s: useCases %v, want %v (bestFor %v)", id, p.UseCases, want, text[id].bestFor)
		}
		ans := res.Pages.Network[id+"/"+p.PrimaryKind]
		if ans == nil || ans.Profile == nil {
			t.Fatalf("%s: no %s network answer", id, p.PrimaryKind)
		}
		if prof := ans.Profile; strOf(prof.HQCountry) != country || strOf(prof.HQCity) != city || !slices.Equal(prof.UseCases, p.UseCases) {
			t.Errorf("%s: profile %v %v %v", id, prof.HQCountry, prof.HQCity, prof.UseCases)
		}
		if hq == nil && !slices.Contains(p.NA, "hqCountry") || slices.Contains(p.NA, "useCases") {
			t.Errorf("%s: na %v", id, p.NA)
		}
	}
	if s := res.Details["shifter"].Provider; strOf(s.HQCountry) != "HK" || strOf(s.HQCity) != "Hong Kong" {
		t.Errorf("shifter: hq %q %q, want HK Hong Kong", strOf(s.HQCountry), strOf(s.HQCity))
	}
}

// providerText is a catalog row's English hq and best_for, which the API no
// longer reads.
type providerText struct {
	hq      *string
	bestFor []string
}

func catalogText(t *testing.T, db *testdb.DB) map[string]providerText {
	t.Helper()
	c := db.Conn(t)
	defer c.Close(context.Background())
	rows, err := c.Query(context.Background(), `SELECT slug, hq, best_for FROM site.providers`)
	if err != nil {
		t.Fatal(err)
	}
	out := map[string]providerText{}
	for rows.Next() {
		var slug string
		var p providerText
		if err := rows.Scan(&slug, &p.hq, &p.bestFor); err != nil {
			t.Fatal(err)
		}
		out[slug] = p
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	return out
}

func strOf(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}

// Migration 0004 takes every use_case code, ISO codes, a BCP 47 language
// and spend bounds, and refuses anything else.
func TestProviderCodesMigrationChecks(t *testing.T) {
	t.Parallel()
	db := testdb.New(t)
	all := make([]string, len(build.UseCases))
	for i, c := range build.UseCases {
		all[i] = c.Code
	}
	db.Exec(t, `INSERT INTO site.providers(slug,name,hq_country,hq_city,use_cases) VALUES ('acme','Acme','LT','Vilnius',$1)`, all)
	db.Exec(t, `INSERT INTO site.reviews(review_id,provider_slug,author,role,rating,title,body,product_kind,use_case,monthly_spend,months_used,
	  verification,posted_at,lang,monthly_spend_min,monthly_spend_max) VALUES ('r1','acme','A','dev',4,'t','b','residential_rotating','web_scraping',
	  '$100',2,'unverified',now(),'pt-BR',100,500)`)
	db.Exec(t, `INSERT INTO site.community_runs(run_id,provider_slug,submitted_by,region,product_kind,plan_tier,collector_version,status,
	  submitted_at,country) VALUES ('run1','acme','x','eu','residential_rotating','starter','1.0','accepted',now(),'DE')`)
	c := db.Conn(t)
	defer c.Close(context.Background())
	for _, bad := range []string{
		`UPDATE site.providers SET use_cases = '{web_scraping,scraping}'`,
		`UPDATE site.providers SET hq_country = 'Lithuania'`,
		`UPDATE site.providers SET hq_country = NULL`,
		`UPDATE site.providers SET hq_city = ' '`,
		`UPDATE site.reviews SET lang = 'English'`,
		`UPDATE site.reviews SET monthly_spend_min = 600`,
		`UPDATE site.reviews SET monthly_spend_max = -1`,
		`UPDATE site.community_runs SET country = 'de'`,
	} {
		if _, err := c.Exec(context.Background(), bad); err == nil || !strings.Contains(err.Error(), "check constraint") {
			t.Errorf("%s: %v, want a check violation", bad, err)
		}
	}
}

// A review serves its language and spend bounds and a run its country;
// without them each is null and listed in its na.
func TestReviewAndRunCodes(t *testing.T) {
	t.Parallel()
	db := testdb.New(t)
	db.Exec(t, `INSERT INTO site.providers(slug,name) VALUES ('acme','Acme');
	INSERT INTO site.reviews(review_id,provider_slug,author,role,rating,title,body,product_kind,use_case,monthly_spend,months_used,
	  verification,posted_at,lang,monthly_spend_min,monthly_spend_max) VALUES
	  ('r1','acme','A','dev',4,'t','b','residential_rotating','web_scraping','$100',2,'unverified',now(),'en',100,500),
	  ('r2','acme','B','dev',5,'t','b','residential_rotating','scraping','$100',2,'unverified',now() - interval '1 day',NULL,NULL,NULL);
	INSERT INTO site.community_runs(run_id,provider_slug,submitted_by,region,product_kind,plan_tier,collector_version,status,submitted_at,country,
	  rejection_reason) VALUES ('run1','acme','x','eu','residential_rotating','starter','1.0','accepted',now(),'DE',NULL),
	  ('run2','acme','x','eu','residential_rotating','starter','1.0','rejected',now() - interval '1 day',NULL,'too_few_requests')`)
	ds := buildFrom(t, db).Dataset
	if len(ds.Reviews) != 2 || len(ds.CommunityRuns) != 2 {
		t.Fatalf("reviews %d runs %d", len(ds.Reviews), len(ds.CommunityRuns))
	}
	r, bare := ds.Reviews[0], ds.Reviews[1]
	if strOf(r.Lang) != "en" || r.SpendMin.F() != 100 || r.SpendMax.F() != 500 || strOf(r.UseCase) != "web_scraping" || slices.Contains(r.NA, "lang") {
		t.Errorf("review %+v", r)
	}
	// A use case that is no code ("scraping") is null (PROFILE-TEXT-REMOVE-1).
	if bare.Lang != nil || bare.SpendMin.OK() || bare.SpendMax.OK() || bare.UseCase != nil {
		t.Errorf("review without codes %+v", bare)
	}
	for _, p := range []string{"lang", "monthlySpendMin", "monthlySpendMax", "useCase"} {
		if !slices.Contains(bare.NA, p) {
			t.Errorf("review na %v lacks %s", bare.NA, p)
		}
	}
	if strOf(ds.CommunityRuns[0].Country) != "DE" || ds.CommunityRuns[1].Country != nil || !slices.Contains(ds.CommunityRuns[1].NA, "country") ||
		ds.CommunityRuns[0].RejectionReason != nil || strOf(ds.CommunityRuns[1].RejectionReason) != "too_few_requests" {
		t.Errorf("runs %+v %+v", ds.CommunityRuns[0], ds.CommunityRuns[1])
	}
}
