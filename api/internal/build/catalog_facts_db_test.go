package build_test

import (
	"context"
	"os"
	"regexp"
	"strings"
	"testing"

	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/testdb"
	"github.com/jackc/pgx/v5"
)

// The catalog facts each network's own site gives (catalog-network-facts.sql)
// and the generated trust figures of the two test days (trust-test-data.sql)
// write separate columns of the same rows.
var (
	factColumns = []string{"hq", "founded", "claim", "best_for",
		"claimed_pool_size", "kyc_verified", "consent_disclosed", "stickiness_advertised"}
	testDataColumns = []string{"blacklist_hit_rate", "stickiness_hold_rate", "leak_checks"}
)

// factGaps are the facts a network's own site does not state, so the catalog
// leaves them null (the PR names each).
var factGaps = map[string][]string{
	"dataimpulse": {"founded"}, "evomi": {"founded"}, "geonode": {"founded"}, "infatica": {"stickiness_advertised"},
	"massive": {"founded"}, "nodemaven": {"hq"}, "proxy-cheap": {"hq", "kyc_verified"},
	"proxy-seller": {"founded", "kyc_verified"}, "shifter": {"hq", "kyc_verified"}, "webshare": {"stickiness_advertised"},
}

const testDataNote = "TEST DATA (generated, not measured)"

func sqlFile(t *testing.T, rel ...string) string {
	t.Helper()
	b, err := os.ReadFile(repoFile(t, rel...))
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

// sqlCode is a script without its comment lines.
func sqlCode(s string) string {
	var out []string
	for _, l := range strings.Split(s, "\n") {
		if !strings.HasPrefix(strings.TrimSpace(l), "--") {
			out = append(out, l)
		}
	}
	return strings.Join(out, "\n")
}

func mentions(code string, cols []string) []string {
	var hit []string
	for _, c := range cols {
		if regexp.MustCompile(`\b` + c + `\b`).MatchString(code) {
			hit = append(hit, c)
		}
	}
	return hit
}

// The facts script names no test-data column and the test-data script no fact
// column.
func TestCatalogFactsAndTestDataColumns(t *testing.T) {
	t.Parallel()
	facts := sqlCode(sqlFile(t, "api", "scripts", "catalog-network-facts.sql"))
	if hit := mentions(facts, testDataColumns); len(hit) > 0 {
		t.Errorf("catalog-network-facts.sql sets test-data columns %v", hit)
	}
	gen := sqlCode(sqlFile(t, "scripts", "scale", "score-data", "trust-test-data.sql"))
	others := append([]string{"residential_authenticity", "datacenter_asn_share", "claim_gap"}, factColumns...)
	if hit := mentions(gen, others); len(hit) > 0 {
		t.Errorf("trust-test-data.sql sets catalog columns %v", hit)
	}
}

// Both scripts are upserts that keep each other's columns, in either order, and
// the API serves every fact and figure for all 18 networks.
func TestCatalogFactsAndTestData(t *testing.T) {
	t.Parallel()
	db := testdb.New(t)
	for id := range networkNames {
		db.Exec(t, "INSERT INTO providers VALUES ($1) ON CONFLICT DO NOTHING", id)
	}
	db.Exec(t, sqlFile(t, "api", "scripts", "catalog-networks.sql"))
	db.Exec(t, `INSERT INTO site.trust_signals(provider_slug,category,blacklist_hit_rate,stickiness_hold_rate,leak_checks,residential_authenticity)
	  VALUES ('oxylabs','residential',0.5,0.5,'[]',0.7)`)
	facts := sqlFile(t, "api", "scripts", "catalog-network-facts.sql")
	db.Exec(t, facts)
	db.Exec(t, facts)
	c := db.Conn(t)
	ctx := context.Background()
	var kept, stray int
	if err := c.QueryRow(ctx, `SELECT
	  (SELECT count(*) FROM site.trust_signals WHERE provider_slug = 'oxylabs' AND blacklist_hit_rate = 0.5
	     AND stickiness_hold_rate = 0.5 AND leak_checks = '[]' AND residential_authenticity = 0.7 AND claimed_pool_size > 0),
	  (SELECT count(*) FROM site.trust_signals WHERE provider_slug <> 'oxylabs'
	     AND (blacklist_hit_rate IS NOT NULL OR stickiness_hold_rate IS NOT NULL OR leak_checks IS NOT NULL))`).Scan(&kept, &stray); err != nil {
		t.Fatal(err)
	}
	if kept != 1 || stray != 0 {
		t.Fatalf("the facts script changed test-data columns: kept %d, stray %d", kept, stray)
	}
	before := factRows(t, c)
	gen := sqlFile(t, "scripts", "scale", "score-data", "trust-test-data.sql")
	db.Exec(t, gen)
	db.Exec(t, gen)
	if after := factRows(t, c); after != before {
		t.Fatalf("the test-data script changed catalog columns:\n%s\n%s", before, after)
	}
	db.Exec(t, facts)
	checkTestData(t, c)
	res := buildFrom(t, db)
	if len(res.Details) != 18 {
		t.Errorf("%d networks, want 18", len(res.Details))
	}
	for id := range networkNames {
		if d := res.Details[id]; d == nil {
			t.Errorf("no network %s", id)
		} else {
			checkServedFacts(t, id, d.Provider)
		}
	}
}

// A fact the facts script leaves null keeps the value the catalog holds.
func TestCatalogFactsKeepHeldValues(t *testing.T) {
	t.Parallel()
	db := testdb.New(t)
	for id := range networkNames {
		db.Exec(t, "INSERT INTO providers VALUES ($1) ON CONFLICT DO NOTHING", id)
	}
	db.Exec(t, sqlFile(t, "api", "scripts", "catalog-networks.sql"))
	db.Exec(t, `UPDATE site.providers SET hq = 'Held HQ', founded = 1999 WHERE framework_provider IN ('shifter', 'evomi')`)
	db.Exec(t, `INSERT INTO site.trust_signals(provider_slug,category,kyc_verified,stickiness_advertised)
	  VALUES ('shifter','residential',true,'held'), ('webshare','residential',true,'held')`)
	db.Exec(t, sqlFile(t, "api", "scripts", "catalog-network-facts.sql"))
	var got string
	if err := db.Conn(t).QueryRow(context.Background(), `SELECT string_agg(r, ' ' ORDER BY r) FROM (
	  SELECT concat_ws('/', slug, hq, founded) AS r FROM site.providers WHERE slug IN ('shifter', 'evomi')
	  UNION ALL SELECT concat_ws('/', provider_slug, kyc_verified, stickiness_advertised)
	  FROM site.trust_signals WHERE provider_slug IN ('shifter', 'webshare')) x`).Scan(&got); err != nil {
		t.Fatal(err)
	}
	want := "evomi/Ermatingen, Switzerland/1999 shifter/Held HQ/2012 shifter/t/a configurable duration (ttl) webshare/f/held"
	if got != want {
		t.Fatalf("held facts %q, want %q", got, want)
	}
}

// factRows is every column but the test-data ones, of both tables.
func factRows(t *testing.T, c *pgx.Conn) string {
	t.Helper()
	var s string
	if err := c.QueryRow(context.Background(), `SELECT
	  (SELECT string_agg(row(slug, hq, founded, claim, best_for, name, tagline)::text, E'\n' ORDER BY slug) FROM site.providers)
	  || E'\n' ||
	  (SELECT string_agg(row(provider_slug, category, claimed_pool_size, kyc_verified, consent_disclosed, stickiness_advertised,
	     residential_authenticity, datacenter_asn_share, claim_gap, is_demo)::text, E'\n' ORDER BY provider_slug) FROM site.trust_signals)`).Scan(&s); err != nil {
		t.Fatal(err)
	}
	return s
}

// checkTestData: 18 marked rows, blocklist rates of 0.2% to 4% with no two
// equal, hold rates of 0.80 to 0.99, and five leak checks each, of which two or
// three networks fail exactly one.
func checkTestData(t *testing.T, c *pgx.Conn) {
	t.Helper()
	var rows, rates, holds, marked, failing, failedChecks int
	if err := c.QueryRow(context.Background(), `SELECT count(*),
	  count(DISTINCT blacklist_hit_rate) FILTER (WHERE blacklist_hit_rate BETWEEN 0.002 AND 0.04),
	  count(*) FILTER (WHERE stickiness_hold_rate BETWEEN 0.80 AND 0.99),
	  count(*) FILTER (WHERE jsonb_array_length(leak_checks) = 5
	    AND (SELECT bool_and(e->>'note' = $1 AND e->>'name' IN ('dns','webrtc','headers','tls','ipv6')) FROM jsonb_array_elements(leak_checks) e)),
	  count(*) FILTER (WHERE leak_checks @> '[{"passed": false}]'),
	  (SELECT count(*) FROM site.trust_signals, jsonb_array_elements(leak_checks) e
	     WHERE category = 'residential' AND NOT is_demo AND e->>'passed' = 'false')
	  FROM site.trust_signals WHERE category = 'residential' AND NOT is_demo`, testDataNote).
		Scan(&rows, &rates, &holds, &marked, &failing, &failedChecks); err != nil {
		t.Fatal(err)
	}
	if rows != 18 || rates != 18 || holds != 18 || marked != 18 || failing < 2 || failing > 3 || failedChecks != failing {
		t.Fatalf("rows %d, distinct rates %d, holds %d, marked %d, failing networks %d, failed checks %d",
			rows, rates, holds, marked, failing, failedChecks)
	}
}

// checkServedFacts: the API serves every fact but the network's named gaps, and
// every generated figure. The claim and the advertised stickiness are catalog
// text it no longer serves (API-PROSE-REMOVAL-1, #962); hq and best_for are
// served as their codes, hqCountry and useCases (PROFILE-TEXT-REMOVE-1).
func checkServedFacts(t *testing.T, id string, p *model.Provider) {
	t.Helper()
	tr := p.Measured.Trust
	served := map[string]bool{
		"hq": p.HQCountry != nil, "founded": p.Founded.OK(),
		"best_for":          len(p.UseCases) > 0,
		"claimed_pool_size": p.Measured.Pool.ClaimedPoolSize.OK() && p.Measured.Pool.ClaimedPoolSize.F() > 0,
		"kyc_verified":      tr.KycVerified != nil, "consent_disclosed": tr.ConsentDisclosed != nil,
	}
	gaps := map[string]bool{}
	for _, f := range factGaps[id] {
		gaps[f] = true
	}
	for _, f := range factColumns {
		if _, text := map[string]bool{"claim": true, "stickiness_advertised": true}[f]; text {
			continue
		}
		if served[f] == gaps[f] {
			t.Errorf("%s: %s served %v, a named gap %v", id, f, served[f], gaps[f])
		}
	}
	if !tr.BlacklistHitRate.OK() || !tr.StickinessHoldRate.OK() || len(tr.LeakChecks) != 5 || tr.LeakChecks[0].Name == nil || tr.LeakChecks[0].Passed == nil {
		t.Errorf("%s: test-data trust %+v", id, tr)
	}
}
