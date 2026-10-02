package store

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
)

// DemoDatasetName is the site.datasets row of the design fixtures.
const DemoDatasetName = "design-demo"

// demoFile is the subset of fixtures/demo/design-demo.json that seeding reads. Nested
// measurement documents stay raw so they are stored byte-for-byte as parsed.
type demoFile struct {
	SchemaVersion    string             `json:"schemaVersion"`
	GeneratedAt      string             `json:"generatedAt"`
	Seed             *int64             `json:"seed"`
	RunID            *string            `json:"runId"`
	CollectorVersion *string            `json:"collectorVersion"`
	WindowHours      *int32             `json:"windowHours"`
	Targets          []demoTarget       `json:"targets"`
	Scopes           []demoScope        `json:"scopes"`
	Weights          map[string]float64 `json:"weights"`
	Profiles         []demoProfile      `json:"profiles"`
	Providers        []demoProvider     `json:"providers"`
	Tape             []demoTape         `json:"tape"`
	Changelog        []demoChange       `json:"changelog"`
	Reviews          []demoReview       `json:"reviews"`
	CommunityRuns    []demoRun          `json:"communityRuns"`
}

type demoTarget struct {
	ID            string  `json:"id"`
	Label         *string `json:"label"`
	Category      *string `json:"category"`
	PassCondition *string `json:"passCondition"`
	Difficulty    *int16  `json:"difficulty"`
}

type demoScope struct {
	ID             string          `json:"id"`
	Label          *string         `json:"label"`
	Summary        *string         `json:"summary"`
	Cadence        *string         `json:"cadence"`
	Targets        json.RawMessage `json:"targets"`
	LiveIPSampling *bool           `json:"liveIpSampling"`
	PoolTracking   *bool           `json:"poolTracking"`
	OverlapAudit   *bool           `json:"overlapAudit"`
	StickinessTest *bool           `json:"stickinessTest"`
	GeoCoverage    *string         `json:"geoCoverage"`
	Rationale      *string         `json:"rationale"`
}

type demoProfile struct {
	ID          string          `json:"id"`
	Label       *string         `json:"label"`
	Description *string         `json:"description"`
	Weights     json.RawMessage `json:"weights"`
}

type demoPlan struct {
	Name        *string  `json:"name"`
	Price       *float64 `json:"price"`
	Gb          *float64 `json:"gb"`
	Ips         *float64 `json:"ips"`
	Note        *string  `json:"note"`
	Recommended *bool    `json:"recommended"`
	DiscountPct *float64 `json:"discountPct"`
}

type demoPricing struct {
	Model             *string    `json:"model"`
	PricePerGb        *float64   `json:"pricePerGb"`
	PricePerGbAtScale *float64   `json:"pricePerGbAtScale"`
	MinCommit         *float64   `json:"minCommit"`
	Trial             *string    `json:"trial"`
	Plans             []demoPlan `json:"plans"`
	CostPer1kSuccess  *float64   `json:"costPer1kSuccess"`
	AvgPayloadKb      *float64   `json:"avgPayloadKb"`
	CostBasis         *string    `json:"costBasis"`
	HeadlinePriceRank *int32     `json:"headlinePriceRank"`
	ValueRank         *int32     `json:"valueRank"`
}

type demoTrust struct {
	ResidentialAuthenticity *float64        `json:"residentialAuthenticity"`
	DatacenterAsnShare      *float64        `json:"datacenterAsnShare"`
	ClaimGap                *float64        `json:"claimGap"`
	BlacklistHitRate        *float64        `json:"blacklistHitRate"`
	StickinessHoldRate      *float64        `json:"stickinessHoldRate"`
	StickinessAdvertised    *string         `json:"stickinessAdvertised"`
	LeakChecks              json.RawMessage `json:"leakChecks"`
	KycVerified             *bool           `json:"kycVerified"`
	ConsentDisclosed        *bool           `json:"consentDisclosed"`
}

type demoMeasurement struct {
	Kind        string          `json:"kind"`
	Product     string          `json:"product"`
	Pool        json.RawMessage `json:"pool"`
	Performance json.RawMessage `json:"performance"`
	Pricing     demoPricing     `json:"pricing"`
	Trust       demoTrust       `json:"trust"`
	Targets     json.RawMessage `json:"targets"`
	Series      json.RawMessage `json:"series"`
	IPSamples   json.RawMessage `json:"ipSamples"`
	Incidents   json.RawMessage `json:"incidents"`
	Geo         json.RawMessage `json:"geo"`
	Asns        json.RawMessage `json:"asns"`
	Overlap     json.RawMessage `json:"overlap"`
}

type demoProduct struct {
	Kind       string  `json:"kind"`
	Name       *string `json:"name"`
	Measured   bool    `json:"measured"`
	URL        *string `json:"url"`
	PriceLabel *string `json:"priceLabel"`
	Note       *string `json:"note"`
}

type demoScore struct {
	Composite    *float64        `json:"composite"`
	Rank         *int32          `json:"rank"`
	PreviousRank *int32          `json:"previousRank"`
	Breakdown    json.RawMessage `json:"breakdown"`
	Grade        *string         `json:"grade"`
}

type demoProvider struct {
	Slug                string                     `json:"slug"`
	Name                string                     `json:"name"`
	Tagline             *string                    `json:"tagline"`
	Kinds               []string                   `json:"kinds"`
	PrimaryKind         *string                    `json:"primaryKind"`
	HQ                  *string                    `json:"hq"`
	Founded             *int32                     `json:"founded"`
	Claim               *string                    `json:"claim"`
	Verdict             *string                    `json:"verdict"`
	Strengths           []string                   `json:"strengths"`
	Weaknesses          []string                   `json:"weaknesses"`
	BestFor             []string                   `json:"bestFor"`
	Products            []demoProduct              `json:"products"`
	MeasuredProduct     *string                    `json:"measuredProduct"`
	ProductMeasurements map[string]demoMeasurement `json:"productMeasurements"`
	Score               demoScore                  `json:"score"`
	Overlap             json.RawMessage            `json:"overlap"`
	// Raw keeps the full object to discover productMeasurements key order.
	Raw json.RawMessage `json:"-"`
}

func (p *demoProvider) UnmarshalJSON(b []byte) error {
	type plain demoProvider
	if err := json.Unmarshal(b, (*plain)(p)); err != nil {
		return err
	}
	p.Raw = append(json.RawMessage(nil), b...)
	return nil
}

// measurementOrder returns productMeasurements keys in document order.
func (p *demoProvider) measurementOrder() ([]string, error) {
	var outer map[string]json.RawMessage
	if err := json.Unmarshal(p.Raw, &outer); err != nil {
		return nil, err
	}
	dec := json.NewDecoder(strings.NewReader(string(outer["productMeasurements"])))
	if _, err := dec.Token(); err != nil {
		return nil, err
	}
	var keys []string
	for dec.More() {
		t, err := dec.Token()
		if err != nil {
			return nil, err
		}
		keys = append(keys, t.(string))
		var skip json.RawMessage
		if err := dec.Decode(&skip); err != nil {
			return nil, err
		}
	}
	return keys, nil
}

type demoTape struct {
	ID           string  `json:"id"`
	Kind         string  `json:"kind"`
	ProviderSlug string  `json:"providerSlug"`
	Product      string  `json:"product"`
	Title        string  `json:"title"`
	Detail       string  `json:"detail"`
	Signal       int16   `json:"signal"`
	Value        string  `json:"value"`
	AgoSeconds   float64 `json:"agoSeconds"`
}

type demoChange struct {
	ID           string   `json:"id"`
	Kind         string   `json:"kind"`
	ProviderSlug *string  `json:"providerSlug"`
	Headline     string   `json:"headline"`
	Detail       string   `json:"detail"`
	Delta        *float64 `json:"delta"`
	Unit         string   `json:"unit"`
	AgoSeconds   float64  `json:"agoSeconds"`
	Severity     string   `json:"severity"`
}

type demoReview struct {
	ID            string  `json:"id"`
	ProviderSlug  string  `json:"providerSlug"`
	Author        string  `json:"author"`
	Role          string  `json:"role"`
	Rating        int16   `json:"rating"`
	Title         string  `json:"title"`
	Body          string  `json:"body"`
	Product       string  `json:"product"`
	UseCase       string  `json:"useCase"`
	MonthlySpend  string  `json:"monthlySpend"`
	MonthsUsed    int32   `json:"monthsUsed"`
	Verification  string  `json:"verification"`
	InvoiceMonth  *string `json:"invoiceMonth"`
	LinkedRunID   *string `json:"linkedRunId"`
	HelpfulCount  int32   `json:"helpfulCount"`
	ProviderReply *string `json:"providerReply"`
	AgoSeconds    float64 `json:"agoSeconds"`
}

type demoRun struct {
	ID               string          `json:"id"`
	ProviderSlug     string          `json:"providerSlug"`
	SubmittedBy      string          `json:"submittedBy"`
	Region           string          `json:"region"`
	Product          string          `json:"product"`
	PlanTier         string          `json:"planTier"`
	CollectorVersion string          `json:"collectorVersion"`
	Status           string          `json:"status"`
	RejectionReason  *string         `json:"rejectionReason"`
	Requests         *int64          `json:"requests"`
	SuccessRate      *float64        `json:"successRate"`
	LatencyP50       *float64        `json:"latencyP50"`
	UniqueIps        *int64          `json:"uniqueIps"`
	OverlapShare     *float64        `json:"overlapShare"`
	Targets          json.RawMessage `json:"targets"`
	SuccessSeries    json.RawMessage `json:"successSeries"`
	LatencySeries    json.RawMessage `json:"latencySeries"`
	DeltaSuccessPts  *float64        `json:"deltaSuccessPts"`
	DeltaLatencyPct  *float64        `json:"deltaLatencyPct"`
	AgoSeconds       float64         `json:"agoSeconds"`
}

// SeedResult reports what seeding did.
type SeedResult struct {
	Skipped      bool           `json:"skipped"`
	Reason       string         `json:"reason,omitempty"`
	SourceSHA256 string         `json:"source_sha256"`
	Rows         map[string]int `json:"rows"`
	Backup       *BackupResult  `json:"backup,omitempty"`
}

func rawOr(r json.RawMessage, fallback string) []byte {
	if len(r) == 0 || string(r) == "null" {
		return []byte(fallback)
	}
	return r
}

// at converts a fixture age to an absolute time against the fixture's generatedAt.
func at(generated time.Time, agoSeconds float64) time.Time {
	return generated.Add(-time.Duration(agoSeconds * float64(time.Second)))
}

// DemoLoaded reports whether this exact fixture file is already loaded, so a
// no-op reseed can skip its backup.
func DemoLoaded(ctx context.Context, q Querier, file string) (bool, error) {
	body, err := os.ReadFile(file)
	if err != nil {
		return false, err
	}
	sum := sha256.Sum256(body)
	var existing *string
	err = q.QueryRow(ctx, "SELECT source_sha256 FROM site.datasets WHERE name=$1", DemoDatasetName).Scan(&existing)
	if errors.Is(err, pgx.ErrNoRows) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	return existing != nil && *existing == hex.EncodeToString(sum[:]), nil
}

// SeedDemo loads the design fixtures into the site schema as is_demo rows. It
// is idempotent: a file whose sha256 matches the loaded one is skipped (unless
// force), otherwise existing demo rows are replaced in one transaction.
// Catalog rows (targets, scopes, weights, ranking profiles) are inserted only
// where missing, so editorial changes are never overwritten.
func SeedDemo(ctx context.Context, conn *pgx.Conn, file string, force bool) (SeedResult, error) {
	body, err := os.ReadFile(file)
	if err != nil {
		return SeedResult{}, err
	}
	sum := sha256.Sum256(body)
	res := SeedResult{SourceSHA256: hex.EncodeToString(sum[:]), Rows: map[string]int{}}
	var doc demoFile
	if err := json.Unmarshal(body, &doc); err != nil {
		return res, fmt.Errorf("parse %s: %w", file, err)
	}
	generated, err := time.Parse(time.RFC3339Nano, doc.GeneratedAt)
	if err != nil {
		return res, fmt.Errorf("generatedAt: %w", err)
	}
	if err := CheckSchema(ctx, conn); err != nil {
		return res, err
	}
	err = pgx.BeginFunc(ctx, conn, func(tx pgx.Tx) error {
		if _, err := tx.Exec(ctx, "SELECT pg_advisory_xact_lock($1)", lockKey); err != nil {
			return err
		}
		var existing *string
		err := tx.QueryRow(ctx, "SELECT source_sha256 FROM site.datasets WHERE name=$1", DemoDatasetName).Scan(&existing)
		if err != nil && !errors.Is(err, pgx.ErrNoRows) {
			return err
		}
		if !force && existing != nil && *existing == res.SourceSHA256 {
			var n int
			if err := tx.QueryRow(ctx, "SELECT count(*) FROM site.providers WHERE is_demo").Scan(&n); err != nil {
				return err
			}
			if n == len(doc.Providers) {
				res.Skipped = true
				res.Reason = "this file is already loaded (same sha256); use --force to reload"
				return nil
			}
		}
		if _, err := deleteDemo(ctx, tx); err != nil {
			return err
		}
		return insertDemo(ctx, tx, &doc, file, res.SourceSHA256, generated, res.Rows)
	})
	return res, err
}

var demoTables = []string{"pricing_plans", "pricing", "trust_signals", "measurements", "provider_products",
	"reviews", "community_runs", "changelog", "tape_events", "providers", "ranking_profiles",
	"score_weights", "scopes", "targets", "datasets"}

func deleteDemo(ctx context.Context, tx pgx.Tx) (map[string]int64, error) {
	out := map[string]int64{}
	for _, t := range demoTables {
		tag, err := tx.Exec(ctx, "DELETE FROM site."+t+" WHERE is_demo")
		if err != nil {
			return nil, fmt.Errorf("delete demo rows from site.%s: %w", t, err)
		}
		out[t] = tag.RowsAffected()
	}
	return out, nil
}

// UnseedDemo deletes every is_demo row.
func UnseedDemo(ctx context.Context, conn *pgx.Conn) (map[string]int64, error) {
	if err := CheckSchema(ctx, conn); err != nil {
		return nil, err
	}
	var out map[string]int64
	err := pgx.BeginFunc(ctx, conn, func(tx pgx.Tx) error {
		if _, err := tx.Exec(ctx, "SELECT pg_advisory_xact_lock($1)", lockKey); err != nil {
			return err
		}
		var err error
		out, err = deleteDemo(ctx, tx)
		return err
	})
	return out, err
}

func insertDemo(ctx context.Context, tx pgx.Tx, doc *demoFile, file, sha string, generated time.Time, rows map[string]int) error {
	exec := func(table, sql string, args ...any) error {
		tag, err := tx.Exec(ctx, sql, args...)
		if err != nil {
			return fmt.Errorf("insert into site.%s: %w", table, err)
		}
		rows[table] += int(tag.RowsAffected())
		return nil
	}
	if err := exec("datasets", `INSERT INTO site.datasets(name,schema_version,generated_at,seed,run_id,collector_version,window_hours,source_file,source_sha256,is_demo)
 VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,true)`, DemoDatasetName, doc.SchemaVersion, generated, doc.Seed, doc.RunID, doc.CollectorVersion, doc.WindowHours, filepath.Base(file), sha); err != nil {
		return err
	}

	// Catalog. A target the framework measures is real reference data; the others exist only
	// for the fixtures and leave with them.
	framework := map[string]bool{}
	if ids, err := collect(ctx, tx, "SELECT target_id FROM public.targets", func(r pgx.Rows, v *string) error { return r.Scan(v) }); err == nil {
		for _, id := range ids {
			framework[id] = true
		}
	} else {
		return err
	}
	for i, t := range doc.Targets {
		if err := exec("targets", `INSERT INTO site.targets(target_id,label,category,pass_condition,difficulty,sort_order,is_demo)
 VALUES ($1,$2,$3,$4,$5,$6,$7) ON CONFLICT (target_id) DO NOTHING`, t.ID, t.Label, t.Category, t.PassCondition, t.Difficulty, i*10, !framework[t.ID]); err != nil {
			return err
		}
	}
	for i, s := range doc.Scopes {
		all := true
		ids := []string{}
		if len(s.Targets) > 0 && s.Targets[0] == '[' {
			all = false
			if err := json.Unmarshal(s.Targets, &ids); err != nil {
				return fmt.Errorf("scope %s targets: %w", s.ID, err)
			}
		}
		if err := exec("scopes", `INSERT INTO site.scopes(product_kind,label,summary,cadence,all_targets,target_ids,live_ip_sampling,pool_tracking,overlap_audit,stickiness_test,geo_coverage,rationale,sort_order,is_demo)
 VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,false) ON CONFLICT (product_kind) DO NOTHING`,
			s.ID, s.Label, s.Summary, s.Cadence, all, ids, s.LiveIPSampling, s.PoolTracking, s.OverlapAudit, s.StickinessTest, s.GeoCoverage, s.Rationale, i*10); err != nil {
			return err
		}
	}
	keys := make([]string, 0, len(doc.Weights))
	for k := range doc.Weights {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		if err := exec("score_weights", `INSERT INTO site.score_weights(component,weight,is_demo) VALUES ($1,$2,false) ON CONFLICT (component) DO NOTHING`, k, doc.Weights[k]); err != nil {
			return err
		}
	}
	for i, p := range doc.Profiles {
		if err := exec("ranking_profiles", `INSERT INTO site.ranking_profiles(profile_id,label,description,weights,sort_order,is_demo) VALUES ($1,$2,$3,$4,$5,false) ON CONFLICT (profile_id) DO NOTHING`,
			p.ID, p.Label, p.Description, rawOr(p.Weights, "{}"), i*10); err != nil {
			return err
		}
	}

	for i, p := range doc.Providers {
		primary := ""
		if p.PrimaryKind != nil {
			primary = *p.PrimaryKind
		}
		order, err := p.measurementOrder()
		if err != nil {
			return fmt.Errorf("provider %s: %w", p.Slug, err)
		}
		// The headline block is the primary product's; keep the overlap only when it differs.
		var headlineOverlap []byte
		if pm, ok := p.ProductMeasurements[primary]; ok && !jsonEqual(pm.Overlap, p.Overlap) {
			headlineOverlap = p.Overlap
		}
		if err := exec("providers", `INSERT INTO site.providers(slug,name,tagline,kinds,primary_kind,measured_product,hq,founded,claim,verdict,strengths,weaknesses,best_for,sort_order,score_composite,score_rank,score_previous_rank,score_grade,score_breakdown,headline_overlap,is_demo)
 VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,true)`,
			p.Slug, p.Name, p.Tagline, nonNil(p.Kinds), p.PrimaryKind, p.MeasuredProduct, p.HQ, p.Founded, p.Claim, p.Verdict,
			nonNil(p.Strengths), nonNil(p.Weaknesses), nonNil(p.BestFor), i*10,
			p.Score.Composite, p.Score.Rank, p.Score.PreviousRank, p.Score.Grade, rawOr(p.Score.Breakdown, "{}"), nullableJSON(headlineOverlap)); err != nil {
			return err
		}
		for j, pr := range p.Products {
			if err := exec("provider_products", `INSERT INTO site.provider_products(provider_slug,product_kind,name,measured,url,price_label,note,sort_order,is_demo) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,true)`,
				p.Slug, pr.Kind, pr.Name, pr.Measured, pr.URL, pr.PriceLabel, pr.Note, j*10); err != nil {
				return err
			}
		}
		for j, cat := range order {
			m := p.ProductMeasurements[cat]
			pc := m.Pricing
			if err := exec("pricing", `INSERT INTO site.pricing(provider_slug,category,model,price_per_gb,price_per_gb_at_scale,min_commit,trial,cost_per_1k_success,avg_payload_kb,cost_basis,headline_price_rank,value_rank,is_demo)
 VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,true)`, p.Slug, cat, pc.Model, pc.PricePerGb, pc.PricePerGbAtScale, pc.MinCommit, pc.Trial, pc.CostPer1kSuccess, pc.AvgPayloadKb, pc.CostBasis, pc.HeadlinePriceRank, pc.ValueRank); err != nil {
				return err
			}
			for k, plan := range pc.Plans {
				if err := exec("pricing_plans", `INSERT INTO site.pricing_plans(provider_slug,category,position,name,price,gb,ips,note,recommended,discount_pct,is_demo) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,true)`,
					p.Slug, cat, k, plan.Name, plan.Price, plan.Gb, plan.Ips, plan.Note, plan.Recommended, plan.DiscountPct); err != nil {
					return err
				}
			}
			var claimed struct {
				ClaimedPoolSize *int64 `json:"claimedPoolSize"`
			}
			_ = json.Unmarshal(m.Pool, &claimed)
			t := m.Trust
			if err := exec("trust_signals", `INSERT INTO site.trust_signals(provider_slug,category,residential_authenticity,datacenter_asn_share,claim_gap,blacklist_hit_rate,stickiness_hold_rate,stickiness_advertised,claimed_pool_size,leak_checks,kyc_verified,consent_disclosed,is_demo)
 VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,true)`, p.Slug, cat, t.ResidentialAuthenticity, t.DatacenterAsnShare, t.ClaimGap, t.BlacklistHitRate, t.StickinessHoldRate, t.StickinessAdvertised, claimed.ClaimedPoolSize, rawOr(t.LeakChecks, "[]"), t.KycVerified, t.ConsentDisclosed); err != nil {
				return err
			}
			var perf struct {
				SuccessRate *float64 `json:"successRate"`
				LatencyP50  *float64 `json:"latencyP50"`
			}
			_ = json.Unmarshal(m.Performance, &perf)
			var pool struct {
				UniqueIps24h *float64 `json:"uniqueIps24h"`
			}
			_ = json.Unmarshal(m.Pool, &pool)
			if err := exec("measurements", `INSERT INTO site.measurements(provider_slug,category,product_kind,position,pool,performance,targets,series,ip_samples,incidents,geo,asns,overlap,success_rate,latency_p50,unique_ips_24h,is_demo)
 VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,true)`, p.Slug, cat, m.Product, j,
				rawOr(m.Pool, "{}"), rawOr(m.Performance, "{}"), rawOr(m.Targets, "[]"), rawOr(m.Series, "{}"), rawOr(m.IPSamples, "[]"),
				rawOr(m.Incidents, "[]"), rawOr(m.Geo, "[]"), rawOr(m.Asns, "[]"), rawOr(m.Overlap, "{}"),
				perf.SuccessRate, perf.LatencyP50, pool.UniqueIps24h); err != nil {
				return err
			}
		}
	}
	for i, t := range doc.Tape {
		if err := exec("tape_events", `INSERT INTO site.tape_events(tape_id,kind,provider_slug,product,title,detail,signal,value,occurred_at,position,is_demo) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,true)`,
			t.ID, t.Kind, t.ProviderSlug, t.Product, t.Title, t.Detail, t.Signal, t.Value, at(generated, t.AgoSeconds), i); err != nil {
			return err
		}
	}
	for i, c := range doc.Changelog {
		if err := exec("changelog", `INSERT INTO site.changelog(change_id,kind,provider_slug,headline,detail,delta,unit,severity,occurred_at,position,is_demo) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,true)`,
			c.ID, c.Kind, c.ProviderSlug, c.Headline, c.Detail, c.Delta, c.Unit, c.Severity, at(generated, c.AgoSeconds), i); err != nil {
			return err
		}
	}
	for i, r := range doc.Reviews {
		if err := exec("reviews", `INSERT INTO site.reviews(review_id,provider_slug,author,role,rating,title,body,product_kind,use_case,monthly_spend,months_used,verification,invoice_month,linked_run_id,helpful_count,provider_reply,posted_at,position,is_demo)
 VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,true)`,
			r.ID, r.ProviderSlug, r.Author, r.Role, r.Rating, r.Title, r.Body, r.Product, r.UseCase, r.MonthlySpend, r.MonthsUsed, r.Verification, r.InvoiceMonth, r.LinkedRunID, r.HelpfulCount, r.ProviderReply, at(generated, r.AgoSeconds), i); err != nil {
			return err
		}
	}
	for i, r := range doc.CommunityRuns {
		if err := exec("community_runs", `INSERT INTO site.community_runs(run_id,provider_slug,submitted_by,region,product_kind,plan_tier,collector_version,status,rejection_reason,requests,success_rate,latency_p50,unique_ips,overlap_share,delta_success_pts,delta_latency_pct,targets,success_series,latency_series,submitted_at,position,is_demo)
 VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,true)`,
			r.ID, r.ProviderSlug, r.SubmittedBy, r.Region, r.Product, r.PlanTier, r.CollectorVersion, r.Status, r.RejectionReason, r.Requests, r.SuccessRate, r.LatencyP50, r.UniqueIps, r.OverlapShare, r.DeltaSuccessPts, r.DeltaLatencyPct,
			rawOr(r.Targets, "[]"), rawOr(r.SuccessSeries, "[]"), rawOr(r.LatencySeries, "[]"), at(generated, r.AgoSeconds), i); err != nil {
			return err
		}
	}
	return nil
}

func nonNil(s []string) []string {
	if s == nil {
		return []string{}
	}
	return s
}

func nullableJSON(b []byte) any {
	if len(b) == 0 {
		return nil
	}
	return b
}

func jsonEqual(a, b json.RawMessage) bool {
	var x, y any
	if json.Unmarshal(a, &x) != nil || json.Unmarshal(b, &y) != nil {
		return false
	}
	xa, _ := json.Marshal(x)
	ya, _ := json.Marshal(y)
	return string(xa) == string(ya)
}
