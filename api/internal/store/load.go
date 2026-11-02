package store

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
)

// Fingerprint is the cheap change detector polled every refresh interval.
type Fingerprint struct {
	BuildID       *int64     `json:"build_id"`
	MaxEventID    *int64     `json:"max_event_id"`
	SiteRevision  int64      `json:"site_revision"`
	SiteUpdatedAt *time.Time `json:"site_updated_at"`
	DemoProviders int64      `json:"demo_providers"`
	Products      int64      `json:"framework_products"`
	TargetsDigest *string    `json:"framework_targets_digest"`
}

// Key is a comparable string form.
func (f Fingerprint) Key() string {
	b, _ := json.Marshal(f)
	return string(b)
}

const fingerprintSQL = `SELECT
 (SELECT build_id FROM public.current_calculation WHERE singleton),
 (SELECT max(event_id) FROM public.test_events),
 (SELECT value FROM site.revision),
 (SELECT max(u) FROM (
   SELECT max(updated_at) u FROM site.datasets UNION ALL SELECT max(updated_at) FROM site.targets
   UNION ALL SELECT max(updated_at) FROM site.scopes UNION ALL SELECT max(updated_at) FROM site.score_weights
   UNION ALL SELECT max(updated_at) FROM site.ranking_profiles UNION ALL SELECT max(updated_at) FROM site.providers
   UNION ALL SELECT max(updated_at) FROM site.provider_products UNION ALL SELECT max(updated_at) FROM site.pricing
   UNION ALL SELECT max(updated_at) FROM site.pricing_plans UNION ALL SELECT max(updated_at) FROM site.trust_signals
   UNION ALL SELECT max(updated_at) FROM site.measurements UNION ALL SELECT max(updated_at) FROM site.reviews
   UNION ALL SELECT max(updated_at) FROM site.community_runs UNION ALL SELECT max(updated_at) FROM site.changelog
   UNION ALL SELECT max(updated_at) FROM site.tape_events) x),
 (SELECT count(*) FROM site.providers WHERE is_demo),
 (SELECT count(*) FROM public.products),
 (SELECT md5(string_agg(target_id || '|' || coalesce(label,'') || '|' || coalesce(pass_condition,'') || '|' || sort_order, ',' ORDER BY target_id)) FROM public.targets)`

// ReadFingerprint runs the change-detection query.
func ReadFingerprint(ctx context.Context, q Querier) (Fingerprint, error) {
	var f Fingerprint
	err := q.QueryRow(ctx, fingerprintSQL).Scan(&f.BuildID, &f.MaxEventID, &f.SiteRevision, &f.SiteUpdatedAt, &f.DemoProviders, &f.Products, &f.TargetsDigest)
	return f, err
}

type DatasetRow struct {
	Name             string
	SchemaVersion    string
	GeneratedAt      time.Time
	Seed             *int64
	RunID            *string
	CollectorVersion *string
	WindowHours      *int32
	IsDemo           bool
}

type TargetRow struct {
	ID            string
	Label         *string
	Category      *string
	PassCondition *string
	Difficulty    *int16
	SortOrder     int32
	IsDemo        bool
}

type FrameworkTarget struct {
	ID            string
	Label         *string
	PassCondition *string
	SortOrder     int32
}

type ScopeRow struct {
	Kind                                                   string
	Label, Summary, Cadence                                *string
	AllTargets                                             bool
	TargetIDs                                              []string
	LiveIPSampling, PoolTracking, OverlapAudit, Stickiness *bool
	GeoCoverage, Rationale                                 *string
	IsDemo                                                 bool
}

type WeightRow struct {
	Component string
	Weight    float64
}

type ProfileRow struct {
	ID          string
	Label       *string
	Description *string
	Weights     json.RawMessage
	IsDemo      bool
}

type ProviderRow struct {
	Slug                            string
	Name                            string
	Tagline                         *string
	Kinds                           []string
	PrimaryKind, MeasuredProduct    *string
	HQ                              *string
	Founded                         *int32
	Claim, Verdict                  *string
	Strengths, Weaknesses, BestFor  []string // nil when the column is NULL
	Website                         *string
	FrameworkProvider               *string
	Listed                          bool
	SortOrder                       int32
	ScoreComposite                  *float64
	ScoreRank, ScorePreviousRank    *int32
	ScoreGrade                      *string
	ScoreBreakdown, HeadlineOverlap json.RawMessage
	IsDemo                          bool
}

type ProductRow struct {
	ProviderSlug     string
	Kind             string
	Name             *string
	Measured         bool
	URL, PriceLabel  *string
	Note             *string
	FrameworkProduct *string
	IsDemo           bool
}

type PricingRow struct {
	ProviderSlug, Category                   string
	Model                                    *string
	PricePerGb, PricePerGbAtScale, MinCommit *float64
	Trial                                    *string
	CostPer1kSuccess, AvgPayloadKb           *float64
	CostBasis                                *string
	HeadlinePriceRank, ValueRank             *int32
	IsDemo                                   bool
}

type PlanRow struct {
	Catalog                json.RawMessage
	ProviderSlug, Category string
	Position               int32
	Name                   *string
	Price, Gb, Ips         *float64
	Note                   *string
	Recommended            *bool
	DiscountPct            *float64
}

type TrustRow struct {
	ProviderSlug, Category                                                                      string
	ResidentialAuthenticity, DatacenterAsnShare, ClaimGap, BlacklistHitRate, StickinessHoldRate *float64
	StickinessAdvertised                                                                        *string
	ClaimedPoolSize                                                                             *int64
	LeakChecks                                                                                  json.RawMessage
	KycVerified, ConsentDisclosed                                                               *bool
	IsDemo                                                                                      bool
}

type MeasurementRow struct {
	ProviderSlug, Category, ProductKind string
	Position                            int32
	Pool, Performance, Targets, Series  json.RawMessage
	IPSamples, Incidents, Geo, Asns     json.RawMessage
	Overlap                             json.RawMessage
	IsDemo                              bool
}

type ReviewRow struct {
	ID, ProviderSlug, Author, Role           string
	Rating                                   int16
	Title, Body, ProductKind, UseCase, Spend string
	MonthsUsed                               int32
	Verification                             string
	InvoiceMonth, LinkedRunID                *string
	HelpfulCount                             int32
	ProviderReply                            *string
	PostedAt                                 time.Time
	IsDemo                                   bool
}

type CommunityRunRow struct {
	ID, ProviderSlug, SubmittedBy, Region, ProductKind, PlanTier, CollectorVersion, Status string
	RejectionReason                                                                        *string
	Requests                                                                               *int64
	SuccessRate, LatencyP50                                                                *float64
	UniqueIps                                                                              *int64
	OverlapShare, DeltaSuccessPts, DeltaLatencyPct                                         *float64
	Targets, SuccessSeries, LatencySeries                                                  json.RawMessage
	SubmittedAt                                                                            time.Time
	IsDemo                                                                                 bool
}

type ChangeRow struct {
	ID, Kind         string
	ProviderSlug     *string
	Headline, Detail string
	Delta            *float64
	Unit, Severity   string
	OccurredAt       time.Time
	IsDemo           bool
}

type TapeRow struct {
	ID, Kind, ProviderSlug, Product, Title, Detail string
	Signal                                         int16
	Value                                          string
	OccurredAt                                     time.Time
	IsDemo                                         bool
}

type FrameworkProduct struct {
	ID       int32
	Provider string
	Name     string
}

type SnapshotRow struct {
	BuildID     int64
	ContentHash string
	Payload     []byte
	full        []byte // Payload with its capped long lists restored (longtail.go)
	// Places is every place list of a capped snapshot, read whole from the
	// build's typed rows (placerows.go); nil when nothing was capped.
	Places map[PlaceWindow][]PlaceRow
}

// Event is one public.test_events row in the framework's /v1/events shape.
type Event struct {
	ID       int64
	At       time.Time
	Kind     string
	Provider *string
	Product  *string
	Country  *string
	Region   *string
	Target   *string
	Signal   int16
	Severity string
	Data     json.RawMessage
}

// Inputs is everything one cache build reads, taken in one REPEATABLE READ
// read-only transaction so the pieces agree with each other.
type Inputs struct {
	Fingerprint        Fingerprint
	ReadAt             time.Time
	Datasets           []DatasetRow
	Targets            []TargetRow
	FrameworkTargets   []FrameworkTarget
	Scopes             []ScopeRow
	Weights            []WeightRow
	Profiles           []ProfileRow
	Providers          []ProviderRow
	Products           []ProductRow
	Pricing            []PricingRow
	Plans              []PlanRow
	Trust              []TrustRow
	Measurements       []MeasurementRow
	Reviews            []ReviewRow
	CommunityRuns      []CommunityRunRow
	Changelog          []ChangeRow
	Tape               []TapeRow
	FrameworkProviders []string
	FrameworkProducts  []FrameworkProduct
	Snapshot           *SnapshotRow
	Events             []Event
	// EventsComplete is true when Events holds every row of test_events.
	EventsComplete bool
}

// EventWindow is how many of the newest events are held in memory.
const EventWindow = 2000

func collect[T any](ctx context.Context, tx pgx.Tx, sql string, scan func(pgx.Rows, *T) error, args ...any) ([]T, error) {
	rows, err := tx.Query(ctx, sql, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []T{}
	for rows.Next() {
		var v T
		if err := scan(rows, &v); err != nil {
			return nil, err
		}
		out = append(out, v)
	}
	return out, rows.Err()
}

// EventColumns is the SELECT list shared by Load and the events fallback query.
const EventColumns = `e.event_id, e.at, e.kind, p.provider, p.name, e.country_code,
 coalesce(r.slug, e.data->>'region'), e.target_id, e.signal, e.severity, e.data
 FROM public.test_events e LEFT JOIN public.products p ON p.product_id = e.product_id
 LEFT JOIN public.runner_regions r ON r.region_id = e.region_id`

// ScanEvent reads one EventColumns row.
func ScanEvent(r pgx.Rows, e *Event) error {
	var country *string
	if err := r.Scan(&e.ID, &e.At, &e.Kind, &e.Provider, &e.Product, &country, &e.Region, &e.Target, &e.Signal, &e.Severity, &e.Data); err != nil {
		return err
	}
	if country != nil {
		c := trimSpace(*country)
		country = &c
	}
	e.Country = country
	return nil
}

func trimSpace(s string) string {
	for len(s) > 0 && s[len(s)-1] == ' ' {
		s = s[:len(s)-1]
	}
	return s
}

// Load reads every input in one consistent read-only snapshot.
func Load(ctx context.Context, db interface {
	BeginTx(ctx context.Context, opts pgx.TxOptions) (pgx.Tx, error)
}) (*Inputs, error) {
	tx, err := db.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.RepeatableRead, AccessMode: pgx.ReadOnly})
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx) //nolint:errcheck
	in := &Inputs{}
	if err := CheckSchema(ctx, tx); err != nil {
		return nil, err
	}
	if in.Fingerprint, err = ReadFingerprint(ctx, tx); err != nil {
		return nil, fmt.Errorf("fingerprint: %w", err)
	}
	if err := tx.QueryRow(ctx, "SELECT now()").Scan(&in.ReadAt); err != nil {
		return nil, err
	}
	steps := []func() error{
		func() (err error) {
			in.Datasets, err = collect(ctx, tx, `SELECT name, schema_version, generated_at, seed, run_id, collector_version, window_hours, is_demo FROM site.datasets ORDER BY is_demo, name`,
				func(r pgx.Rows, v *DatasetRow) error {
					return r.Scan(&v.Name, &v.SchemaVersion, &v.GeneratedAt, &v.Seed, &v.RunID, &v.CollectorVersion, &v.WindowHours, &v.IsDemo)
				})
			return
		},
		func() (err error) {
			in.Targets, err = collect(ctx, tx, `SELECT target_id, label, category, pass_condition, difficulty, sort_order, is_demo FROM site.targets ORDER BY sort_order, target_id`,
				func(r pgx.Rows, v *TargetRow) error {
					return r.Scan(&v.ID, &v.Label, &v.Category, &v.PassCondition, &v.Difficulty, &v.SortOrder, &v.IsDemo)
				})
			return
		},
		func() (err error) {
			in.FrameworkTargets, err = collect(ctx, tx, `SELECT target_id, label, pass_condition, sort_order FROM public.targets ORDER BY sort_order, target_id`,
				func(r pgx.Rows, v *FrameworkTarget) error {
					return r.Scan(&v.ID, &v.Label, &v.PassCondition, &v.SortOrder)
				})
			return
		},
		func() (err error) {
			in.Scopes, err = collect(ctx, tx, `SELECT product_kind, label, summary, cadence, all_targets, target_ids, live_ip_sampling, pool_tracking, overlap_audit, stickiness_test, geo_coverage, rationale, is_demo FROM site.scopes ORDER BY sort_order, product_kind`,
				func(r pgx.Rows, v *ScopeRow) error {
					return r.Scan(&v.Kind, &v.Label, &v.Summary, &v.Cadence, &v.AllTargets, &v.TargetIDs, &v.LiveIPSampling, &v.PoolTracking, &v.OverlapAudit, &v.Stickiness, &v.GeoCoverage, &v.Rationale, &v.IsDemo)
				})
			return
		},
		func() (err error) {
			in.Weights, err = collect(ctx, tx, `SELECT component, weight FROM site.score_weights`,
				func(r pgx.Rows, v *WeightRow) error { return r.Scan(&v.Component, &v.Weight) })
			return
		},
		func() (err error) {
			in.Profiles, err = collect(ctx, tx, `SELECT profile_id, label, description, weights, is_demo FROM site.ranking_profiles ORDER BY sort_order, profile_id`,
				func(r pgx.Rows, v *ProfileRow) error {
					return r.Scan(&v.ID, &v.Label, &v.Description, &v.Weights, &v.IsDemo)
				})
			return
		},
		func() (err error) {
			in.Providers, err = collect(ctx, tx, `SELECT slug, name, tagline, kinds, primary_kind, measured_product, hq, founded, claim, verdict, strengths, weaknesses, best_for, website, framework_provider, listed, sort_order, score_composite, score_rank, score_previous_rank, score_grade, score_breakdown, headline_overlap, is_demo FROM site.providers ORDER BY sort_order, slug`,
				func(r pgx.Rows, v *ProviderRow) error {
					return r.Scan(&v.Slug, &v.Name, &v.Tagline, &v.Kinds, &v.PrimaryKind, &v.MeasuredProduct, &v.HQ, &v.Founded, &v.Claim, &v.Verdict, &v.Strengths, &v.Weaknesses, &v.BestFor, &v.Website, &v.FrameworkProvider, &v.Listed, &v.SortOrder, &v.ScoreComposite, &v.ScoreRank, &v.ScorePreviousRank, &v.ScoreGrade, &v.ScoreBreakdown, &v.HeadlineOverlap, &v.IsDemo)
				})
			return
		},
		func() (err error) {
			in.Products, err = collect(ctx, tx, `SELECT provider_slug, product_kind, name, measured, url, price_label, note, framework_product, is_demo FROM site.provider_products ORDER BY provider_slug, sort_order, product_kind`,
				func(r pgx.Rows, v *ProductRow) error {
					return r.Scan(&v.ProviderSlug, &v.Kind, &v.Name, &v.Measured, &v.URL, &v.PriceLabel, &v.Note, &v.FrameworkProduct, &v.IsDemo)
				})
			return
		},
		func() (err error) {
			in.Pricing, err = collect(ctx, tx, `SELECT provider_slug, category, model, price_per_gb, price_per_gb_at_scale, min_commit, trial, cost_per_1k_success, avg_payload_kb, cost_basis, headline_price_rank, value_rank, is_demo FROM site.pricing`,
				func(r pgx.Rows, v *PricingRow) error {
					return r.Scan(&v.ProviderSlug, &v.Category, &v.Model, &v.PricePerGb, &v.PricePerGbAtScale, &v.MinCommit, &v.Trial, &v.CostPer1kSuccess, &v.AvgPayloadKb, &v.CostBasis, &v.HeadlinePriceRank, &v.ValueRank, &v.IsDemo)
				})
			return
		},
		func() (err error) {
			in.Plans, err = collect(ctx, tx, `SELECT provider_slug, category, position, name, price, gb, ips, note, recommended, discount_pct, catalog FROM site.pricing_plans ORDER BY provider_slug, category, position`,
				func(r pgx.Rows, v *PlanRow) error {
					return r.Scan(&v.ProviderSlug, &v.Category, &v.Position, &v.Name, &v.Price, &v.Gb, &v.Ips, &v.Note, &v.Recommended, &v.DiscountPct, &v.Catalog)
				})
			return
		},
		func() (err error) {
			in.Trust, err = collect(ctx, tx, `SELECT provider_slug, category, residential_authenticity, datacenter_asn_share, claim_gap, blacklist_hit_rate, stickiness_hold_rate, stickiness_advertised, claimed_pool_size, leak_checks, kyc_verified, consent_disclosed, is_demo FROM site.trust_signals`,
				func(r pgx.Rows, v *TrustRow) error {
					return r.Scan(&v.ProviderSlug, &v.Category, &v.ResidentialAuthenticity, &v.DatacenterAsnShare, &v.ClaimGap, &v.BlacklistHitRate, &v.StickinessHoldRate, &v.StickinessAdvertised, &v.ClaimedPoolSize, &v.LeakChecks, &v.KycVerified, &v.ConsentDisclosed, &v.IsDemo)
				})
			return
		},
		func() (err error) {
			in.Measurements, err = collect(ctx, tx, `SELECT provider_slug, category, product_kind, position, pool, performance, targets, series, ip_samples, incidents, geo, asns, overlap, is_demo FROM site.measurements ORDER BY provider_slug, position, category`,
				func(r pgx.Rows, v *MeasurementRow) error {
					return r.Scan(&v.ProviderSlug, &v.Category, &v.ProductKind, &v.Position, &v.Pool, &v.Performance, &v.Targets, &v.Series, &v.IPSamples, &v.Incidents, &v.Geo, &v.Asns, &v.Overlap, &v.IsDemo)
				})
			return
		},
		func() (err error) {
			in.Reviews, err = collect(ctx, tx, `SELECT review_id, provider_slug, author, role, rating, title, body, product_kind, use_case, monthly_spend, months_used, verification, invoice_month, linked_run_id, helpful_count, provider_reply, posted_at, is_demo FROM site.reviews ORDER BY posted_at DESC, position NULLS LAST, review_id`,
				func(r pgx.Rows, v *ReviewRow) error {
					return r.Scan(&v.ID, &v.ProviderSlug, &v.Author, &v.Role, &v.Rating, &v.Title, &v.Body, &v.ProductKind, &v.UseCase, &v.Spend, &v.MonthsUsed, &v.Verification, &v.InvoiceMonth, &v.LinkedRunID, &v.HelpfulCount, &v.ProviderReply, &v.PostedAt, &v.IsDemo)
				})
			return
		},
		func() (err error) {
			in.CommunityRuns, err = collect(ctx, tx, `SELECT run_id, provider_slug, submitted_by, region, product_kind, plan_tier, collector_version, status, rejection_reason, requests, success_rate, latency_p50, unique_ips, overlap_share, delta_success_pts, delta_latency_pct, targets, success_series, latency_series, submitted_at, is_demo FROM site.community_runs ORDER BY submitted_at DESC, position NULLS LAST, run_id`,
				func(r pgx.Rows, v *CommunityRunRow) error {
					return r.Scan(&v.ID, &v.ProviderSlug, &v.SubmittedBy, &v.Region, &v.ProductKind, &v.PlanTier, &v.CollectorVersion, &v.Status, &v.RejectionReason, &v.Requests, &v.SuccessRate, &v.LatencyP50, &v.UniqueIps, &v.OverlapShare, &v.DeltaSuccessPts, &v.DeltaLatencyPct, &v.Targets, &v.SuccessSeries, &v.LatencySeries, &v.SubmittedAt, &v.IsDemo)
				})
			return
		},
		func() (err error) {
			in.Changelog, err = collect(ctx, tx, `SELECT change_id, kind, provider_slug, headline, detail, delta, unit, severity, occurred_at, is_demo FROM site.changelog ORDER BY occurred_at DESC, position NULLS LAST, change_id`,
				func(r pgx.Rows, v *ChangeRow) error {
					return r.Scan(&v.ID, &v.Kind, &v.ProviderSlug, &v.Headline, &v.Detail, &v.Delta, &v.Unit, &v.Severity, &v.OccurredAt, &v.IsDemo)
				})
			return
		},
		func() (err error) {
			in.Tape, err = collect(ctx, tx, `SELECT tape_id, kind, provider_slug, product, title, detail, signal, value, occurred_at, is_demo FROM site.tape_events ORDER BY occurred_at DESC, position NULLS LAST, tape_id`,
				func(r pgx.Rows, v *TapeRow) error {
					return r.Scan(&v.ID, &v.Kind, &v.ProviderSlug, &v.Product, &v.Title, &v.Detail, &v.Signal, &v.Value, &v.OccurredAt, &v.IsDemo)
				})
			return
		},
		func() (err error) {
			in.FrameworkProviders, err = collect(ctx, tx, `SELECT id FROM public.providers ORDER BY id`,
				func(r pgx.Rows, v *string) error { return r.Scan(v) })
			return
		},
		func() (err error) {
			in.FrameworkProducts, err = collect(ctx, tx, `SELECT product_id, provider, name FROM public.products ORDER BY provider, name`,
				func(r pgx.Rows, v *FrameworkProduct) error { return r.Scan(&v.ID, &v.Provider, &v.Name) })
			return
		},
		func() error {
			var s SnapshotRow
			err := tx.QueryRow(ctx, `SELECT s.build_id, s.content_hash, s.payload::text FROM public.calculated_snapshots s
 JOIN public.current_calculation c ON c.build_id = s.build_id WHERE c.singleton`).Scan(&s.BuildID, &s.ContentHash, &s.Payload)
			if errors.Is(err, pgx.ErrNoRows) {
				return nil
			}
			if err != nil {
				return err
			}
			in.Snapshot = &s
			if err := s.readLongTail(ctx, tx); err != nil {
				return err
			}
			return s.readPlaceRows(ctx, tx)
		},
		func() (err error) {
			in.Events, err = collect(ctx, tx, `SELECT `+EventColumns+` ORDER BY e.at DESC, e.event_id DESC LIMIT $1`, ScanEvent, EventWindow+1)
			if err == nil {
				in.EventsComplete = len(in.Events) <= EventWindow
				if !in.EventsComplete {
					in.Events = in.Events[:EventWindow]
				}
			}
			return
		},
	}
	for _, step := range steps {
		if err := step(); err != nil {
			return nil, err
		}
	}
	return in, nil
}
