package build_test

import (
	"bytes"
	"context"
	"encoding/json"
	"math"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/Zeus-Development/proxy-index/api/internal/build"
	"github.com/Zeus-Development/proxy-index/api/internal/config"
	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/Zeus-Development/proxy-index/api/internal/server"
	"github.com/Zeus-Development/proxy-index/api/internal/store"
	"github.com/Zeus-Development/proxy-index/api/internal/testdb"
	"github.com/jackc/pgx/v5/pgxpool"
)

func repoFile(t testing.TB, rel ...string) string {
	t.Helper()
	f := config.FindUp(filepath.Join(rel...))
	if f == "" {
		t.Fatalf("%s not found above the test directory", filepath.Join(rel...))
	}
	return f
}

// testCatalog is test-only site data: target presentation for the two
// framework targets plus a catalog-only target, and the rows a fixture loader
// once marked is_demo (a network, its pricing and a target), which the API
// must never serve.
const testCatalog = `INSERT INTO site.targets(target_id,label,category,pass_condition,difficulty,sort_order) VALUES
 ('generic_http','Generic HTTP','Baseline',NULL,1,0),('generic_https','Generic HTTPS','Baseline',NULL,1,10),
 ('google_search','Google Search','Search','Organic results render without a CAPTCHA.',3,20);
INSERT INTO site.targets(target_id,label,sort_order,is_demo) VALUES ('ticketing_queue','Ticketing queue',30,true);
INSERT INTO site.providers(slug,name,kinds,primary_kind,is_demo) VALUES ('nimbus-residential','Nimbus','{residential}','residential',true);
INSERT INTO site.pricing(provider_slug,category,model,price_per_gb,is_demo) VALUES ('nimbus-residential','residential','per_gb',2.5,true);
INSERT INTO site.tape_events(tape_id,kind,provider_slug,product,title,detail,occurred_at,is_demo) VALUES ('demo-1','pool','nimbus-residential','residential','Demo','x',now(),true);`

// catalogued is a migrated scratch database with testCatalog loaded.
func catalogued(t testing.TB) *testdb.DB {
	t.Helper()
	db := testdb.New(t)
	db.Exec(t, testCatalog)
	return db
}

// marketViews is the dataset's measured market, one fleet view per range.
func marketViews(t testing.TB, res *build.Result) []build.FleetView {
	t.Helper()
	var views []build.FleetView
	if err := json.Unmarshal(res.Dataset.MeasuredMarket.Views, &views); err != nil {
		t.Fatal(err)
	}
	return views
}

// valueRank is slug's value (cost) rank in the range's rankings, 0 when unranked.
func valueRank(res *build.Result, rangeID, slug string) int {
	for _, n := range res.Pages.Rankings[rangeID].Networks {
		if n.Slug == slug && n.ValueRank != nil {
			return *n.ValueRank
		}
	}
	return 0
}

func buildFrom(t testing.TB, db *testdb.DB) *build.Result {
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
	res, err := build.Build(in)
	if err != nil {
		t.Fatal(err)
	}
	return res
}

func generic(t testing.TB, v any) any {
	t.Helper()
	var b []byte
	switch x := v.(type) {
	case []byte:
		b = x
	default:
		var err error
		if b, err = server.Marshal(v); err != nil {
			t.Fatal(err)
		}
	}
	dec := json.NewDecoder(bytes.NewReader(b))
	dec.UseNumber()
	var out any
	if err := dec.Decode(&out); err != nil {
		t.Fatal(err)
	}
	return out
}

// A database with only the framework's schema and nothing published (no
// `siteapi migrate` yet) builds an honest empty dataset: no networks, the
// framework's targets, and null fleet figures listed as N/A.
func TestAFrameworkOnlyDatabaseBuildsAnHonestEmptyDataset(t *testing.T) {
	t.Parallel()
	db := testdb.NewFramework(t)
	res := buildFrom(t, db)
	ds := res.Dataset
	if len(ds.Providers) != 0 || len(ds.Tape) != 0 || len(ds.Reviews) != 0 || ds.Simulated {
		t.Fatalf("%d providers, %d tape, simulated=%v", len(ds.Providers), len(ds.Tape), ds.Simulated)
	}
	if len(ds.Targets) != 2 || len(ds.Scopes) != 0 || len(ds.Profiles) != 0 {
		t.Fatalf("%d targets %d scopes %d profiles", len(ds.Targets), len(ds.Scopes), len(ds.Profiles))
	}
	g := generic(t, ds).(map[string]any)
	totals := g["totals"].(map[string]any)
	if totals["medianLatency"] != nil || totals["providers"].(json.Number).String() != "0" {
		t.Fatalf("totals without providers: %v", totals)
	}
	if !contains(ds.NA, "totals.medianLatency") || !contains(ds.NA, "snapshot.snapshotId") {
		t.Fatalf("dataset na %v", ds.NA)
	}
	if len(res.SiteGaps) != 1 {
		t.Fatalf("site gaps %q", res.SiteGaps)
	}
}

func contains(list []string, v string) bool {
	for _, x := range list {
		if x == v {
			return true
		}
	}
	return false
}

// partialSnapshot is a minimal v2 snapshot: one product, HTTP measured, HTTPS
// not, no ready score, a v2 country breakdown for US only, one census. Its
// cohort is a hash, as the framework's are, so no other text holds it
// (testscale.AsV4Answers replaces it).
const partialSnapshot = `{
 "schema_version": 2, "snapshot_id": "77", "implementation_id": "calc-test",
 "as_of": "2026-10-01T12:00:00+00:00", "generated_at": "2026-10-01T12:00:05+00:00",
 "readiness": "partial", "resolution_seconds": 300, "refresh_seconds": 60,
 "periods": {"24h": {"start": "2026-09-30T12:00:00+00:00", "end": "2026-10-01T12:00:00+00:00", "hours": 24},
   "7d": {"start": "2026-09-24T12:00:00+00:00", "end": "2026-10-01T12:00:00+00:00", "hours": 168},
   "30d": {"start": "2026-09-01T12:00:00+00:00", "end": "2026-10-01T12:00:00+00:00", "hours": 720}},
 "methodology": {"id": "m-test", "settings": {"weights": {"reliability": 22, "speed": 14, "uptime": 10, "coverage": 12, "diversity": 8, "dcAsns": 10, "authenticity": 10, "value": 14}, "profiles": {}}},
 "products": [{
  "id": "1:5e0c2a9d1b7f4e3a8c61", "product_id": 1, "provider": "acme", "product": "residential", "name": "Acme", "kind": "residential", "cohort": "5e0c2a9d1b7f4e3a8c61",
  "criteria": {"kind": "residential", "country": null, "runner_location": null, "concurrency": 25, "session_policy": "rotate"},
  "census": [{"country": "US", "region": "fra1", "finished_at": "2026-10-01T06:10:00+00:00", "requests": 5000, "successes": 4900, "unique_ips": 4100, "duration_s": 300, "stop_reason": "unique_ratio"}],
  "outages": [],
  "windows": {
   "24h": {"period": {"start": "2026-09-30T12:00:00+00:00", "end": "2026-10-01T12:00:00+00:00", "hours": 24}, "readiness": "partial",
     "metrics": {"completed": 400, "successes": 380, "errors": 20, "body_bytes": 6000, "success_rate": 0.95, "timeout_rate": 0.01, "coverage": 0.5, "last_observed": null, "error_categories": {},
       "latency_ms": {"p50": 410, "p90": 900, "p95": 1100, "p99": 2000}, "challenge_rate": null, "block_rate": null, "uptime": 0.99},
     "pool": {"unique_ips": 300, "new_ips": 300, "churn_rate": null, "recurring_share": null, "countries": 1, "regions": 3, "cities": 9, "asns": 4,
       "datacenter_asn_share": 0.02, "observations": 380, "geo": [], "countries_breakdown": [{"country": "United States", "country_code": "US", "ips": 300, "observations": 380}],
       "asns_breakdown": [{"asn": 7922, "name": "Comcast", "usage_type": "consumer", "ips": 200, "observations": 250}]},
     "uptime": {"batches": 48, "up": 47, "value": 0.99, "downtime_s": 864, "basis": "outage time", "countries": 1},
     "breakdowns": {"country": [{"country_code": "US", "country": "United States", "ips": 300, "observations": 380, "completed": 400, "successes": 380, "challenges": 0, "blocks": 0, "success_rate": 0.95, "p50": 410, "p90": 900, "p99": 2000}]},
     "targets": {"generic_http": {"readiness": "measured", "metrics": {"completed": 400, "successes": 380, "errors": 20, "body_bytes": 6000, "success_rate": 0.95, "timeout_rate": 0.01, "coverage": 0.5, "last_observed": null, "error_categories": {}, "latency_ms": {"p50": 410, "p90": 900, "p95": 1100, "p99": 2000}, "challenge_rate": 0.01, "block_rate": 0.0}}},
     "scores": {"balanced": {"score": null, "grade": null, "rank": null, "previous_rank": null, "state": "incomplete", "scored_at": null, "valid_until": null, "reasons": ["uptime not ready"],
       "components": {"reliability": {"value": 87.5, "state": "ready", "reason": null, "inputs": null}, "speed": {"value": null, "state": "incomplete", "reason": "x", "inputs": null}}}},
     "series": [{"at": "2026-10-01T11:00:00+00:00", "completed": 100, "successes": 95, "success_rate": 0.95, "latency_ms": {"p50": 400, "p90": 880, "p95": 1000, "p99": 1900}, "unique_ips": 90},
                {"at": "2026-10-01T11:30:00+00:00", "completed": 0, "successes": 0, "success_rate": null, "latency_ms": {"p50": null, "p90": null, "p95": null, "p99": null}, "unique_ips": null}],
     "success_delta": 0.01, "overlap": {"exclusive_share": null, "shared_share": null, "compared_products": 0}, "ip_samples": [], "evidence": {}, "unavailable": {}, "roles": {}},
   "7d": {"period": {"start": "2026-09-24T12:00:00+00:00", "end": "2026-10-01T12:00:00+00:00", "hours": 168}, "readiness": "partial",
     "metrics": {"completed": 2800, "successes": 2660, "errors": 140, "body_bytes": 42000, "success_rate": 0.95, "timeout_rate": 0.01, "coverage": 0.5, "last_observed": null, "error_categories": {}, "latency_ms": {"p50": 420, "p90": 910, "p95": 1100, "p99": 2100}},
     "pool": {"unique_ips": 1200, "new_ips": 900, "churn_rate": 0.75, "recurring_share": 0.1, "countries": 1, "regions": 3, "cities": 9, "asns": 4, "datacenter_asn_share": 0.02, "observations": 2660, "geo": [], "countries_breakdown": [], "asns_breakdown": []},
     "targets": {}, "scores": {}, "series": [], "success_delta": null, "overlap": {"exclusive_share": null, "shared_share": null, "compared_products": 0}, "ip_samples": [], "evidence": {}, "unavailable": {}, "roles": {}},
   "30d": {"period": {"start": "2026-09-01T12:00:00+00:00", "end": "2026-10-01T12:00:00+00:00", "hours": 720}, "readiness": "partial",
     "metrics": {"completed": 12000, "successes": 11400, "errors": 600, "body_bytes": 180000, "success_rate": 0.95, "timeout_rate": 0.01, "coverage": 0.5, "last_observed": null, "error_categories": {},
       "latency_ms": {"p50": 430, "p90": 920, "p95": 1100, "p99": 2200}, "challenge_rate": null, "block_rate": null, "uptime": 0.995},
     "pool": {"unique_ips": 5000, "new_ips": 4000, "churn_rate": 0.8, "recurring_share": 0.2, "countries": 2, "regions": 5, "cities": 20, "asns": 9, "datacenter_asn_share": 0.03, "observations": 11400, "geo": [],
       "countries_breakdown": [{"country": "United States", "country_code": "US", "ips": 4000, "observations": 9000}, {"country": "Germany", "country_code": "DE", "ips": 1000, "observations": 2400}],
       "asns_breakdown": [{"asn": 7922, "name": "Comcast", "usage_type": "consumer", "ips": 3000, "observations": 7000}, {"asn": 16509, "name": "Amazon", "usage_type": "hosting", "ips": 150, "observations": 300}]},
     "uptime": {"batches": 1440, "up": 1430, "value": 0.995, "downtime_s": 12960, "basis": "outage time", "countries": 1},
     "breakdowns": {"country": [{"country_code": "US", "country": "United States", "ips": 4000, "observations": 9000, "completed": 9500, "successes": 9030, "challenges": 0, "blocks": 0, "success_rate": 0.9505, "p50": 425, "p90": 900, "p99": 2100}]},
     "targets": {"generic_http": {"readiness": "measured", "metrics": {"completed": 12000, "successes": 11400, "errors": 600, "body_bytes": 180000, "success_rate": 0.95, "timeout_rate": 0.01, "coverage": 0.5, "last_observed": null, "error_categories": {}, "latency_ms": {"p50": 430, "p90": 920, "p95": 1100, "p99": 2200}, "challenge_rate": 0.01, "block_rate": 0.0}}},
     "scores": {"balanced": {"score": null, "grade": null, "rank": null, "previous_rank": null, "state": "incomplete", "scored_at": null, "valid_until": null, "reasons": [],
       "components": {"reliability": {"value": 87.5, "state": "ready", "reason": null, "inputs": null}, "speed": {"value": 61.2, "state": "ready", "reason": null, "inputs": null}}}},
     "series": [{"at": "2026-09-30T00:00:00+00:00", "completed": 400, "successes": 380, "success_rate": 0.95, "latency_ms": {"p50": 420, "p90": 900, "p95": 1000, "p99": 2000}, "unique_ips": 300}],
     "success_delta": -0.002, "overlap": {"exclusive_share": null, "shared_share": null, "compared_products": 0}, "ip_samples": [], "evidence": {}, "unavailable": {}, "roles": {}}
  }
 }],
 "fleet": [], "overlap": [], "targets": [], "runs": [], "events": [], "traffic": {"period": {"start": "2026-09-01T12:00:00+00:00", "end": "2026-10-01T12:00:00+00:00"}, "by_test": {"census": {"runs": 0, "requests": 0, "bytes_up": 0, "bytes_down": 0}, "pulse": {"runs": 0, "requests": 0, "bytes_up": 0, "bytes_down": 0}}},
 "dbip_release": null
}`

// acmeCatalog is partialSnapshot's network in the catalog and two of its
// test events, once it is published.
const acmeCatalog = `INSERT INTO site.providers(slug,name,hq,website,framework_provider,kinds,primary_kind) VALUES ('acme-proxies','Acme Proxies','Berlin, DE','https://acme.example','acme','{residential}','residential');
INSERT INTO site.pricing(provider_slug,category,model,price_per_gb,avg_payload_kb) VALUES ('acme-proxies','residential','per_gb',4.5,180);
INSERT INTO site.trust_signals(provider_slug,category,kyc_verified,claimed_pool_size) VALUES ('acme-proxies','residential',true,1000000);
INSERT INTO test_events(at,kind,product_id,country_code,signal,severity,data) VALUES
 ('2026-10-01T11:58:00+00','pulse_finished',(SELECT product_id FROM products WHERE provider='acme'),'US',1,'info','{"requests": 100, "success_rate": 0.97, "p50": 400}'),
 ('2026-10-01T11:59:00+00','down',(SELECT product_id FROM products WHERE provider='acme'),'DE',-1,'major','{"source": "pulse"}')`

func TestNAManifestForAPartiallyMeasuredProvider(t *testing.T) {
	t.Parallel()
	db := catalogued(t)
	db.PublishV4(t, []byte(partialSnapshot))
	db.Exec(t, acmeCatalog)
	res := buildFrom(t, db)
	p := res.Details["acme-proxies"]
	if p == nil {
		t.Fatalf("measured provider missing; have %v", keys(res.Details))
	}
	prov := p.Provider
	if prov.Source != "measured" || prov.Framework == nil || prov.Framework.Provider != "acme" || prov.Name != "Acme Proxies" {
		t.Fatalf("identity: %+v", prov)
	}
	na := map[string]bool{}
	for _, x := range prov.NA {
		na[x] = true
	}
	mustNA := []string{
		"founded", "claim", "bestFor", "score.composite", "score.rank", "score.grade", "score.breakdown.uptime",
		"performance.ttfbP50", "performance.captchaRate", "pool.requestsPerIp", "pricing.plans", "pricing.trial",
		"pricing.minCommit", "trust.blacklistHitRate", "trust.leakChecks", "trust.consentDisclosed",
		"targets[generic_https].successRate", "targets[google_search].latencyP50", "targets[generic_http].delta24h",
		"ipSamples", "series.pricePerGb90d", "overlap.overlaps", "overlap.exclusiveShare",
		"overlap.likelyUpstream", "reviews.mean", "productMeasurements.residential.pricing.plans",
	}
	for _, x := range mustNA {
		if !na[x] {
			t.Errorf("%s should be N/A; na = %v", x, prov.NA)
		}
	}
	mustHave := []string{"hq", "website", "tagline", "verdict", "strengths", "pricing.pricePerGb", "pricing.costPer1kSuccess",
		"pricing.costBasis", "pricing.model", "trust.kycVerified", "pool.claimedPoolSize", "performance.successRate",
		"performance.uptimePct", "trust.datacenterAsnShare", "score.breakdown.reliability", "targets[generic_http].successRate",
		// A v2 snapshot publishes outages: an empty list is zero outages, and the
		// 24h outage time gives the weighted downtime.
		"incidents", "productMeasurements.residential.incidents", "performance.downtimeMinutes24h",
		// The geo rows are the network list's country rows, the country
		// breakdown's (US, with its latency), not the pool list's (US and DE).
		"geo[].latencyP50"}
	for _, x := range mustHave {
		if na[x] {
			t.Errorf("%s has a value but is listed as N/A", x)
		}
	}
	// Every listed path really is null or empty, and every null is listed.
	g := generic(t, prov).(map[string]any)
	for _, x := range prov.NA {
		if !strings.Contains(x, "[") && !nullOrEmpty(g, x) {
			t.Errorf("%s listed as N/A but has a value", x)
		}
	}
	nulls := map[string]bool{}
	collectNulls(g, "", nulls)
	for x := range nulls {
		if x == "framework" || strings.HasPrefix(x, "coverageEvidence.") {
			continue
		}
		if !na[x] {
			t.Errorf("null at %s is not in na", x)
		}
	}
	if got := prov.Pricing.CostPer1kSuccess.F(); got != 0.8131 { // 180 KB x 1,000 = 0.172 GB at $4.50, / 0.95
		t.Errorf("derived cost per 1k = %v", got)
	}
	if prov.Performance.UptimePct.F() != 99.5 || prov.Pool.ClaimedPoolSize.F() != 1e6 {
		t.Errorf("uptime %v claimed %v", prov.Performance.UptimePct, prov.Pool.ClaimedPoolSize)
	}
	if prov.Verdict == nil || !strings.Contains(*prov.Verdict, "The latest census covers 1 country: 4,100 unique IPs from 5,000 requests. An overall score is not available yet.") {
		t.Errorf("verdict %v", prov.Verdict)
	}
	if prov.Series.Success24h[0].T.F() != 60 {
		t.Errorf("series ages are minutes: got %v", prov.Series.Success24h[0].T)
	}
	// A v4 build's coverage evidence is the dataset's City x ASN lists.
	evidence := cityAsnLists(t, res)
	for _, r := range build.Ranges {
		if _, ok := evidence["city_asn acme-proxies "+prov.PrimaryKind+" "+r.ID]; !ok {
			t.Errorf("%s coverage evidence not attached", r.ID)
		}
	}
	// Coverage lists the provider with sectioned N/A fields.
	var cov *build.CoverageEntity
	for i := range res.Coverage.Providers {
		if res.Coverage.Providers[i].ID == "acme-proxies" {
			cov = &res.Coverage.Providers[i]
		}
	}
	if cov == nil || cov.NACount != len(prov.NA) || len(cov.Sections["pricing"]) == 0 || len(cov.Sections["identity"]) == 0 {
		t.Fatalf("coverage entry %+v", cov)
	}
	if res.Coverage.Summary.ProvidersBySource["measured"] != 1 || !res.Coverage.Summary.SnapshotPublished {
		t.Fatalf("coverage summary %+v", res.Coverage.Summary)
	}
	// Measured tape rows lead, newest first, mapped like tape.ts.
	tape := res.Dataset.Tape
	if len(tape) < 2 || tape[0].Source != "measured" || tape[0].Title != "Down in DE" || tape[1].Title != "Pulse in US" || tape[1].Value != "97%" {
		t.Fatalf("measured tape: %+v", tape[:2])
	}
	if tape[1].Detail != "Acme Proxies · 100 requests · p50 400 ms" || tape[1].ProviderSlug != "acme-proxies" {
		t.Fatalf("pulse row %+v", tape[1])
	}
	if res.Dataset.Snapshot.SnapshotID == nil || *res.Dataset.Snapshot.SnapshotID != "77" || res.Dataset.GeneratedAt != "2026-10-01T12:00:00+00:00" {
		t.Fatalf("snapshot meta %+v", res.Dataset.Snapshot)
	}
	// The fleet is the measured network alone: leftover is_demo rows are not served.
	if res.Dataset.Totals.Providers.F() != 1 || res.Dataset.Totals.Requests24h != prov.Performance.Requests24h || len(res.Dataset.Providers) != 1 {
		t.Fatalf("totals %+v", res.Dataset.Totals)
	}
	if w := p.Windows["residential"]["24h"]; w.UptimePct.F() != 99 || w.SuccessDeltaPts.OK() {
		t.Fatalf("measured 24h window %+v", w)
	}
}

func keys[V any](m map[string]V) []string {
	out := []string{}
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

func nullOrEmpty(g map[string]any, path string) bool {
	var v any = g
	for _, seg := range strings.Split(path, ".") {
		m, ok := v.(map[string]any)
		if !ok {
			return false
		}
		v = m[seg]
	}
	switch x := v.(type) {
	case nil:
		return true
	case []any:
		return len(x) == 0
	case map[string]any:
		return len(x) == 0
	}
	return false
}

func collectNulls(v any, path string, out map[string]bool) {
	switch x := v.(type) {
	case nil:
		out[path] = true
	case map[string]any:
		for k, c := range x {
			if k == "na" {
				continue
			}
			p := k
			if path != "" {
				p = path + "." + k
			}
			collectNulls(c, p, out)
		}
	case []any:
		for _, c := range x {
			label := ""
			if m, ok := c.(map[string]any); ok {
				if strings.HasSuffix(path, "targets") {
					label, _ = m["target"].(string)
				}
				if strings.HasSuffix(path, "products") {
					label, _ = m["kind"].(string)
				}
			}
			collectNulls(c, path+"["+label+"]", out)
		}
	}
}

// Keep the pgxpool import used by helpers in other test files of this package.
var _ *pgxpool.Pool

// measuredOnlyV4 builds from a database the way staging looks before any
// catalog rows: the framework's published snapshot, as payload v4, and
// nothing else (no catalog scopes, profiles or pricing). With
// SITEAPI_TEST_DUMP=<file> it also writes the /v1/dataset body there, for an
// offline `SITE_API_DATASET_FILE` site build over measured-only data.
func measuredOnlyV4(t *testing.T, snapshot []byte) *build.Result {
	t.Helper()
	return measuredBuild(t, snapshot, (*testdb.DB).PublishV4)
}

// measuredOnly is measuredOnlyV4 of the snapshot as it is, before payload
// v4: the whole build longtail_db_test.go compares a capped one with.
func measuredOnly(t *testing.T, snapshot []byte) *build.Result {
	t.Helper()
	return measuredBuild(t, snapshot, (*testdb.DB).Publish)
}

func measuredBuild(t *testing.T, snapshot []byte, publish func(*testdb.DB, testing.TB, []byte) int64) *build.Result {
	t.Helper()
	db := testdb.New(t)
	publish(db, t, snapshot)
	res := buildFrom(t, db)
	if out := os.Getenv("SITEAPI_TEST_DUMP"); out != "" {
		body, err := server.Marshal(res.Dataset)
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(out, body, 0o644); err != nil {
			t.Fatal(err)
		}
	}
	return res
}

func TestMeasuredOnlyDatasetHasNoPriceFigures(t *testing.T) {
	t.Parallel()
	// The fixture as v2 with outages, so the dump also shows real outage bands.
	res := measuredOnlyV4(t, withOutages(t))
	ds := res.Dataset
	if ds.Totals.Incidents24h != 2 {
		t.Errorf("incidents24h over the measured fleet: %v", ds.Totals.Incidents24h)
	}
	if len(ds.Providers) != 1 || ds.Providers[0].Source != "measured" || ds.Simulated {
		t.Fatalf("providers %d, simulated %v", len(ds.Providers), ds.Simulated)
	}
	for _, r := range build.Ranges {
		if rank := valueRank(res, r.ID, "shifter"); rank != 0 {
			t.Errorf("%s value rank %d with no prices", r.ID, rank)
		}
	}
	for _, v := range marketViews(t, res) {
		if v.Fleet.Basis != "measured" {
			t.Errorf("%s fleet basis %q", v.Range.ID, v.Fleet.Basis)
		}
		if v.Cheapest.Cost.OK() || v.Dearest.Cost.OK() {
			t.Errorf("%s has a price pick without prices: %+v / %+v", v.Range.ID, v.Cheapest, v.Dearest)
		}
		if v.Bills.Best != nil && v.Bills.Best.Bill.OK() {
			t.Errorf("%s has a bill without prices: %+v", v.Range.ID, v.Bills.Best)
		}
	}
}

// Staging once the framework publishes every-request snapshots: a priced
// measured network is the whole fleet, so it fills the cost boards and bills.
func TestMeasuredOnlyEveryRequestDatasetIsPriced(t *testing.T) {
	t.Parallel()
	snapshot, err := os.ReadFile(repoFile(t, "fixtures", "measured", "snapshot-every-request.json"))
	if err != nil {
		t.Fatal(err)
	}
	res := measuredOnlyV4(t, snapshot)
	for _, r := range build.Ranges {
		if rank := valueRank(res, r.ID, "shifter"); rank != 1 {
			t.Errorf("%s value rank %d, want 1", r.ID, rank)
		}
	}
	for _, v := range marketViews(t, res) {
		if v.Cheapest.Cost != 0.75 || v.Bills.Best == nil || !v.Bills.Best.Bill.OK() {
			t.Errorf("%s fleet view: cheapest %v, bills %+v", v.Range.ID, v.Cheapest.Cost, v.Bills.Best)
		}
	}
}

func TestReviewProductLinkPreservesMeasuredPrices(t *testing.T) {
	t.Parallel()
	snapshot, err := os.ReadFile(repoFile(t, "fixtures", "measured", "snapshot-every-request.json"))
	if err != nil {
		t.Fatal(err)
	}
	db := catalogued(t)
	db.PublishV4(t, snapshot)
	before := buildFrom(t, db).Details["shifter"].Provider
	catalog, err := os.ReadFile(repoFile(t, "api", "scripts", "catalog-shifter.sql"))
	if err != nil {
		t.Fatal(err)
	}
	db.Exec(t, string(catalog))
	db.Exec(t, string(catalog)) // Reapplying the catalog correction is safe.
	after := buildFrom(t, db).Details["shifter"].Provider
	if len(after.Products) != len(before.Products) || model.SV(after.Products[0].URL) != "https://shifter.io/services/residential-proxies" {
		t.Fatalf("catalog did not restore the existing product's link: %+v", after.Products)
	}
	if model.SV(after.Products[0].PriceLabel) != model.SV(before.Products[0].PriceLabel) {
		t.Errorf("adding a link discarded the measured price: %v → %v", before.Products[0].PriceLabel, after.Products[0].PriceLabel)
	}
	if after.Website != nil || after.Performance.SuccessRate != before.Performance.SuccessRate || after.Pricing.PricePerGbAtScale != before.Pricing.PricePerGbAtScale {
		t.Error("restoring the product link changed unrelated provider fields")
	}
}

// withCityAsn is the every-request fixture with one City x ASN row in every
// window.
func withCityAsn(t *testing.T) []byte {
	t.Helper()
	raw, err := os.ReadFile(repoFile(t, "fixtures", "measured", "snapshot-every-request.json"))
	if err != nil {
		t.Fatal(err)
	}
	var snapshot map[string]any
	if err := json.Unmarshal(raw, &snapshot); err != nil {
		t.Fatal(err)
	}
	rows := []any{map[string]any{
		"country_code": "DE", "country": "Germany", "region_code": "BE", "region": "Berlin", "city": "Berlin",
		"asn": 3320, "name": "Deutsche Telekom", "usage_type": "residential", "ips": 12, "observations": 20,
		"completed": 20, "successes": 20, "challenges": 0, "blocks": 0, "success_rate": 1,
		"p50": 50, "p90": 100, "p99": 200,
	}}
	for _, entry := range snapshot["products"].([]any) {
		product := entry.(map[string]any)
		for _, entry := range product["windows"].(map[string]any) {
			window := entry.(map[string]any)
			window["breakdowns"].(map[string]any)["city_asn"] = rows
		}
	}
	changed, err := json.Marshal(snapshot)
	if err != nil {
		t.Fatal(err)
	}
	return changed
}

func TestCoverageNetworkIntersectionsPassThroughToEveryReviewWindow(t *testing.T) {
	t.Parallel()
	res := measuredOnlyV4(t, withCityAsn(t))
	provider := res.Details["shifter"].Provider
	// A v4 build's coverage evidence is the dataset's City x ASN lists.
	expected := `{"total":1,"rows":[["DE","Germany","Berlin","Berlin",3320,"Deutsche Telekom","residential",12,50]]}`
	evidence := cityAsnLists(t, res)
	for _, r := range build.Ranges {
		if received := string(evidence["city_asn shifter "+provider.PrimaryKind+" "+r.ID]); received != expected {
			t.Errorf("%s lost coverage network evidence: %s", r.ID, received)
		}
	}
}

// Rows a fixture loader once marked is_demo are never served: not as a
// network, a target, a tape row or in the measured market.
func TestLeftoverDemoRowsAreNotServed(t *testing.T) {
	t.Parallel()
	db := catalogued(t)
	snapshot, err := os.ReadFile(repoFile(t, "fixtures", "measured", "snapshot.json"))
	if err != nil {
		t.Fatal(err)
	}
	db.PublishV4(t, snapshot)
	res := buildFrom(t, db)
	if len(res.Dataset.Providers) != 1 || res.Details["nimbus-residential"] != nil || res.Pages.Network["nimbus-residential/residential"] != nil {
		t.Fatalf("a demo network is served: %v", keys(res.Details))
	}
	for _, x := range res.Dataset.Targets {
		if x.ID == "ticketing_queue" {
			t.Error("a demo target is served")
		}
	}
	for _, x := range res.Dataset.Tape {
		if x.ID == "demo-1" {
			t.Error("a demo tape row is served")
		}
	}
	views := marketViews(t, res)
	if len(views) != 3 {
		t.Fatalf("market windows %d, want 3", len(views))
	}
	for _, v := range views {
		measured := res.Details["shifter"].Windows["residential"][v.Range.ID]
		if v.Fleet.Basis != "measured" || v.Fleet.Requests != measured.Requests || v.Fleet.UniqueIps != measured.UniqueIps {
			t.Errorf("%s includes non-measured figures: %+v", v.Range.ID, v.Fleet)
		}
		if v.Fastest.Provider == nil || v.Fastest.Provider.Slug != "shifter" {
			t.Errorf("fastest must come from measured networks: %+v", v.Fastest)
		}
		if v.MostReliable.Provider == nil || v.MostReliable.Provider.Slug != "shifter" {
			t.Errorf("reliability must come from measured networks: %+v", v.MostReliable)
		}
		if v.Cheapest.Provider != nil || v.Dearest.Provider != nil {
			t.Errorf("unpriced measured network inherited demo pricing")
		}
		if v.Bills.SpreadX.OK() {
			t.Errorf("unpriced measured network inherited a demo spread")
		}
	}
	p := res.Details["shifter"].Provider
	if res.Dataset.MeasuredMarket.Authenticity.OK() != p.Trust.ResidentialAuthenticity.OK() || (p.Trust.ResidentialAuthenticity.OK() && math.Abs(res.Dataset.MeasuredMarket.Authenticity.F()-p.Trust.ResidentialAuthenticity.F()) > 1e-12) {
		t.Errorf("authenticity is not the measured country match")
	}
	if res.Dataset.MeasuredMarket.TargetCount != 2 {
		t.Errorf("target count includes catalog-only targets: %v", res.Dataset.MeasuredMarket.TargetCount)
	}
}

// withEarlyTrend is the every-request fixture with a second observed bucket
// in its 24h series, five minutes after its first.
func withEarlyTrend(t *testing.T) []byte {
	t.Helper()
	raw, err := os.ReadFile(repoFile(t, "fixtures", "measured", "snapshot-every-request.json"))
	if err != nil {
		t.Fatal(err)
	}
	// The fixture starts with one observed bucket. Add a second recent bucket
	// to exercise the early line rather than only the first-sample dot.
	var doc map[string]any
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatal(err)
	}
	product := doc["products"].([]any)[0].(map[string]any)
	windows := product["windows"].(map[string]any)
	day := windows["24h"].(map[string]any)
	series := day["series"].([]any)
	var sample map[string]any
	for _, row := range series {
		item := row.(map[string]any)
		if item["success_rate"] != nil {
			sample = item
			break
		}
	}
	if sample == nil {
		t.Fatal("fixture has no observed sample")
	}
	copyRow := map[string]any{}
	for key, value := range sample {
		copyRow[key] = value
	}
	at, err := time.Parse(time.RFC3339Nano, sample["at"].(string))
	if err != nil {
		t.Fatal(err)
	}
	copyRow["at"] = at.Add(5 * time.Minute).Format(time.RFC3339Nano)
	copyRow["success_rate"] = 0.95
	day["series"] = append(series, copyRow)
	raw, err = json.Marshal(doc)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func TestMeasuredMarketDrawsFromFirstObservedSamples(t *testing.T) {
	t.Parallel()
	db := catalogued(t)
	db.PublishV4(t, withEarlyTrend(t))
	res := buildFrom(t, db)
	for _, view := range marketViews(t, res) {
		if len(view.HeadlineSpark.Values) < 2 {
			t.Fatalf("%s has no early trend: %+v", view.Range.ID, view.HeadlineSpark)
		}
		for _, value := range view.HeadlineSpark.Values {
			if !value.OK() {
				t.Fatalf("%s still includes empty leading history", view.Range.ID)
			}
		}
	}
}
