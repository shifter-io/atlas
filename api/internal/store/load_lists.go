package store

import (
	"context"
	"errors"
	"fmt"

	"github.com/jackc/pgx/v5"
)

// The optional site reads; a read whose table or columns are missing is empty.
var (
	readTargets   = siteRead{"targets", []string{"target_id", "label", "category", "pass_condition", "difficulty", "sort_order"}, "sort_order, target_id", nil}
	readScopes    = siteRead{"scopes", []string{"product_kind", "label", "summary", "all_targets", "target_ids", "live_ip_sampling", "pool_tracking", "overlap_audit", "stickiness_test", "geo_coverage"}, "sort_order, product_kind", nil}
	readWeights   = siteRead{"score_weights", []string{"component", "weight"}, "component", nil}
	readProfiles  = siteRead{"ranking_profiles", []string{"profile_id", "label", "description", "weights"}, "sort_order, profile_id", nil}
	readProviders = siteRead{"providers", []string{"slug", "name", "tagline", "kinds", "primary_kind", "measured_product", "founded", "claim", "verdict",
		"strengths", "weaknesses", "website", "framework_provider", "listed"}, "sort_order, slug",
		[]laterColumn{{"hq_country", "text"}, {"hq_city", "text"}, {"use_cases", "text[]"}}}
	readProducts = siteRead{"provider_products", []string{"provider_slug", "product_kind", "name", "measured", "url", "price_label", "note", "framework_product"}, "provider_slug, sort_order, product_kind", nil}
	readPricing  = siteRead{"pricing", []string{"provider_slug", "category", "model", "price_per_gb", "price_per_gb_at_scale", "min_commit", "trial",
		"cost_per_1k_success", "avg_payload_kb", "cost_basis", "headline_price_rank", "value_rank"}, "provider_slug, category",
		[]laterColumn{{"trial_price", "double precision"}, {"trial_credit", "double precision"}, {"trial_gb", "double precision"},
			{"trial_days", "integer"}, {"trial_needs_card", "boolean"}}}
	readPlans = siteRead{"pricing_plans", []string{"provider_slug", "category", "position", "name", "price", "gb", "ips", "note", "recommended", "discount_pct", "catalog"}, "provider_slug, category, position", nil}
	readTrust = siteRead{"trust_signals", []string{"provider_slug", "category", "residential_authenticity", "datacenter_asn_share", "claim_gap", "blacklist_hit_rate",
		"stickiness_hold_rate", "stickiness_advertised", "claimed_pool_size", "leak_checks", "kyc_verified", "consent_disclosed"}, "provider_slug, category", nil}
	readReviews = siteRead{"reviews", []string{"review_id", "provider_slug", "author", "role", "rating", "title", "body", "product_kind", "use_case",
		"months_used", "verification", "invoice_month", "linked_run_id", "helpful_count", "provider_reply", "posted_at"}, "posted_at DESC, position NULLS LAST, review_id",
		[]laterColumn{{"lang", "text"}, {"monthly_spend_min", "double precision"}, {"monthly_spend_max", "double precision"}}}
	readRuns = siteRead{"community_runs", []string{"run_id", "provider_slug", "submitted_by", "product_kind", "plan_tier", "collector_version", "status",
		"rejection_reason", "requests", "success_rate", "latency_p50", "unique_ips", "overlap_share", "delta_success_pts", "delta_latency_pct", "targets",
		"success_series", "latency_series", "submitted_at"}, "submitted_at DESC, position NULLS LAST, run_id", []laterColumn{{"country", "text"}}}
	readChangelog = siteRead{"changelog", []string{"change_id", "kind", "provider_slug", "headline", "detail", "delta", "unit", "severity", "occurred_at"}, "occurred_at DESC, position NULLS LAST, change_id", nil}
	readTape      = siteRead{"tape_events", []string{"tape_id", "kind", "provider_slug", "product", "title", "detail", "signal", "value", "occurred_at"}, "occurred_at DESC, position NULLS LAST, tape_id", nil}
	// readPriceHistory is the newest site table (migration 0003).
	readPriceHistory = siteRead{"pricing_history", []string{"provider_slug", "category", "effective_from", "price_per_gb", "price_per_gb_at_scale", "source"},
		"provider_slug, category, effective_from", nil}

	siteReads = []siteRead{readTargets, readScopes, readWeights, readProfiles, readProviders, readProducts, readPricing, readPlans, readTrust,
		readReviews, readRuns, readChangelog, readTape, readPriceHistory}
)

// readSite reads one optional site table, or nothing when the layout cannot serve it.
func readSite[T any](ctx context.Context, tx pgx.Tx, l SiteLayout, r siteRead, scan func(pgx.Rows, *T) error) ([]T, error) {
	if len(r.missing(l)) > 0 {
		return []T{}, nil
	}
	out, err := collect(ctx, tx, r.sql(l), scan)
	if err != nil {
		return nil, fmt.Errorf("site.%s: %w", r.table, err)
	}
	return out, nil
}

// The statements of LoadEvents and LoadLists that take no layout.
const (
	nowSQL                = `SELECT now()`
	frameworkTargetsSQL   = `SELECT target_id, label, pass_condition, sort_order, engine FROM public.targets ORDER BY sort_order, target_id`
	frameworkProvidersSQL = `SELECT id FROM public.providers ORDER BY id`
	frameworkProductsSQL  = `SELECT product_id, provider, name FROM public.products ORDER BY provider, name`
)

// ErrBeforeV4 is LoadLists' error when the current build is older than
// payload v4 (its head has no networks, or another schema_version): the API
// reads only v4 builds.
var ErrBeforeV4 = errors.New("the current build is older than payload v4")

// TxBeginner starts the read-only transactions of Load and LoadEvents (a
// pgxpool.Pool or a pgx.Conn).
type TxBeginner interface {
	BeginTx(ctx context.Context, opts pgx.TxOptions) (pgx.Tx, error)
}

// LoadEvents reads only what an events-only change refreshes, in one
// REPEATABLE READ read-only transaction: the events exactly as Load reads
// them (EventsRead, Events, Notable and their completeness) and the newest
// event id (Fingerprint.MaxEventID, the fingerprint's events part), which is
// EventsRead's. Every other field is empty.
func LoadEvents(ctx context.Context, db TxBeginner) (*Inputs, error) {
	tx, err := db.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.RepeatableRead, AccessMode: pgx.ReadOnly})
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx) //nolint:errcheck
	in := &Inputs{}
	if err := in.loadEvents(ctx, tx); err != nil {
		return nil, fmt.Errorf("events: %w", err)
	}
	if id := in.EventsRead.MaxID; id > 0 {
		in.Fingerprint.MaxEventID = &id
	}
	return in, nil
}

// Load reads every input in one consistent read-only snapshot, every list
// afresh (LoadLists).
func Load(ctx context.Context, db TxBeginner) (*Inputs, error) { return LoadLists(ctx, db, nil) }

// LoadLists reads every input in one consistent read-only snapshot, holding
// the typed lists in lists between calls (nil: none held). Only the
// framework schema is required; site tables that are missing read as empty
// (Inputs.SiteGaps names them). A v4 build's network lists read their rows
// through db when a build or a request needs them: db stays open while the
// Inputs, or a build made from them, are used. A current build older than
// payload v4 is ErrBeforeV4. K2 deletes lists, never drops them, so the
// snapshot keeps seeing the rows of the build it read.
func LoadLists(ctx context.Context, db TxBeginner, lists *Lists) (*Inputs, error) {
	tx, err := db.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.RepeatableRead, AccessMode: pgx.ReadOnly})
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx) //nolint:errcheck
	in := &Inputs{}
	l, err := in.loadHead(ctx, tx)
	if err != nil {
		return nil, err
	}
	steps := in.catalogSteps(ctx, tx, l)
	steps = append(steps, in.providerSteps(ctx, tx, l)...)
	steps = append(steps, in.communitySteps(ctx, tx, l)...)
	steps = append(steps, in.frameworkSteps(ctx, tx)...)
	steps = append(steps,
		func() error { return in.loadSnapshot(ctx, db, tx, lists) },
		func() error { return in.loadRanks(ctx, tx) },
		func() error { return in.loadMethodologies(ctx, tx) },
		func() error { return in.loadEvents(ctx, tx) },
		func() error { return in.loadTapeHeads(ctx, tx) },
	)
	for _, step := range steps {
		if err := step(); err != nil {
			return nil, err
		}
	}
	return in, nil
}

// loadHead reads the site layout, the fingerprint and the read time, the
// first reads of LoadLists.
func (in *Inputs) loadHead(ctx context.Context, tx pgx.Tx) (SiteLayout, error) {
	l, err := ReadSiteLayout(ctx, tx)
	if err != nil {
		return l, fmt.Errorf("site layout: %w", err)
	}
	in.SiteGaps = siteGaps(l, siteReads)
	if in.Fingerprint, err = readFingerprint(ctx, tx, l); err != nil {
		return l, fmt.Errorf("fingerprint: %w", err)
	}
	return l, tx.QueryRow(ctx, nowSQL).Scan(&in.ReadAt)
}

// catalogSteps reads the targets (site and framework), scopes, weights and
// ranking profiles.
func (in *Inputs) catalogSteps(ctx context.Context, tx pgx.Tx, l SiteLayout) []func() error {
	return []func() error{
		func() (err error) {
			in.Targets, err = readSite(ctx, tx, l, readTargets, func(r pgx.Rows, v *TargetRow) error {
				return r.Scan(&v.ID, &v.Label, &v.Category, &v.PassCondition, &v.Difficulty, &v.SortOrder)
			})
			return
		},
		func() (err error) {
			in.FrameworkTargets, err = collect(ctx, tx, frameworkTargetsSQL,
				func(r pgx.Rows, v *FrameworkTarget) error {
					return r.Scan(&v.ID, &v.Label, &v.PassCondition, &v.SortOrder, &v.Engine)
				})
			return
		},
		func() (err error) {
			in.Scopes, err = readSite(ctx, tx, l, readScopes, func(r pgx.Rows, v *ScopeRow) error {
				return r.Scan(&v.Kind, &v.Label, &v.Summary, &v.AllTargets, &v.TargetIDs, &v.LiveIPSampling, &v.PoolTracking, &v.OverlapAudit, &v.Stickiness, &v.GeoCoverage)
			})
			return
		},
		func() (err error) {
			in.Weights, err = readSite(ctx, tx, l, readWeights, func(r pgx.Rows, v *WeightRow) error { return r.Scan(&v.Component, &v.Weight) })
			return
		},
		func() (err error) {
			in.Profiles, err = readSite(ctx, tx, l, readProfiles, func(r pgx.Rows, v *ProfileRow) error {
				return r.Scan(&v.ID, &v.Label, &v.Description, &v.Weights)
			})
			return
		},
	}
}

// providerSteps reads the providers with their products, pricing, plans,
// price history and trust signals.
func (in *Inputs) providerSteps(ctx context.Context, tx pgx.Tx, l SiteLayout) []func() error {
	return []func() error{
		func() (err error) {
			in.Providers, err = readSite(ctx, tx, l, readProviders, func(r pgx.Rows, v *ProviderRow) error {
				return r.Scan(&v.Slug, &v.Name, &v.Tagline, &v.Kinds, &v.PrimaryKind, &v.MeasuredProduct, &v.Founded, &v.Claim, &v.Verdict, &v.Strengths, &v.Weaknesses, &v.Website, &v.FrameworkProvider, &v.Listed, &v.HQCountry, &v.HQCity, &v.UseCases)
			})
			return
		},
		func() (err error) {
			in.Products, err = readSite(ctx, tx, l, readProducts, func(r pgx.Rows, v *ProductRow) error {
				return r.Scan(&v.ProviderSlug, &v.Kind, &v.Name, &v.Measured, &v.URL, &v.PriceLabel, &v.Note, &v.FrameworkProduct)
			})
			return
		},
		func() (err error) {
			in.Pricing, err = readSite(ctx, tx, l, readPricing, func(r pgx.Rows, v *PricingRow) error {
				return r.Scan(&v.ProviderSlug, &v.Category, &v.Model, &v.PricePerGb, &v.PricePerGbAtScale, &v.MinCommit, &v.Trial, &v.CostPer1kSuccess, &v.AvgPayloadKb, &v.CostBasis, &v.HeadlinePriceRank, &v.ValueRank,
					&v.TrialPrice, &v.TrialCredit, &v.TrialGb, &v.TrialDays, &v.TrialNeedsCard)
			})
			return
		},
		func() (err error) {
			in.Plans, err = readSite(ctx, tx, l, readPlans, func(r pgx.Rows, v *PlanRow) error {
				return r.Scan(&v.ProviderSlug, &v.Category, &v.Position, &v.Name, &v.Price, &v.Gb, &v.Ips, &v.Note, &v.Recommended, &v.DiscountPct, &v.Catalog)
			})
			return
		},
		func() (err error) {
			in.PricingHistory, err = readSite(ctx, tx, l, readPriceHistory, func(r pgx.Rows, v *PricingHistoryRow) error {
				return r.Scan(&v.ProviderSlug, &v.Category, &v.EffectiveFrom, &v.PricePerGb, &v.PricePerGbAtScale, &v.Source)
			})
			return
		},
		func() (err error) {
			in.Trust, err = readSite(ctx, tx, l, readTrust, func(r pgx.Rows, v *TrustRow) error {
				return r.Scan(&v.ProviderSlug, &v.Category, &v.ResidentialAuthenticity, &v.DatacenterAsnShare, &v.ClaimGap, &v.BlacklistHitRate, &v.StickinessHoldRate, &v.StickinessAdvertised, &v.ClaimedPoolSize, &v.LeakChecks, &v.KycVerified, &v.ConsentDisclosed)
			})
			return
		},
	}
}

// communitySteps reads the reviews, community runs, changelog and tape.
func (in *Inputs) communitySteps(ctx context.Context, tx pgx.Tx, l SiteLayout) []func() error {
	return []func() error{
		func() (err error) {
			in.Reviews, err = readSite(ctx, tx, l, readReviews, func(r pgx.Rows, v *ReviewRow) error {
				return r.Scan(&v.ID, &v.ProviderSlug, &v.Author, &v.Role, &v.Rating, &v.Title, &v.Body, &v.ProductKind, &v.UseCase, &v.MonthsUsed, &v.Verification, &v.InvoiceMonth, &v.LinkedRunID, &v.HelpfulCount, &v.ProviderReply, &v.PostedAt, &v.Lang, &v.SpendMin, &v.SpendMax)
			})
			return
		},
		func() (err error) {
			in.CommunityRuns, err = readSite(ctx, tx, l, readRuns, func(r pgx.Rows, v *CommunityRunRow) error {
				return r.Scan(&v.ID, &v.ProviderSlug, &v.SubmittedBy, &v.ProductKind, &v.PlanTier, &v.CollectorVersion, &v.Status, &v.RejectionReason, &v.Requests, &v.SuccessRate, &v.LatencyP50, &v.UniqueIps, &v.OverlapShare, &v.DeltaSuccessPts, &v.DeltaLatencyPct, &v.Targets, &v.SuccessSeries, &v.LatencySeries, &v.SubmittedAt, &v.Country)
			})
			return
		},
		func() (err error) {
			in.Changelog, err = readSite(ctx, tx, l, readChangelog, func(r pgx.Rows, v *ChangeRow) error {
				return r.Scan(&v.ID, &v.Kind, &v.ProviderSlug, &v.Headline, &v.Detail, &v.Delta, &v.Unit, &v.Severity, &v.OccurredAt)
			})
			return
		},
		func() (err error) {
			in.Tape, err = readSite(ctx, tx, l, readTape, func(r pgx.Rows, v *TapeRow) error {
				return r.Scan(&v.ID, &v.Kind, &v.ProviderSlug, &v.Product, &v.Title, &v.Detail, &v.Signal, &v.Value, &v.OccurredAt)
			})
			return
		},
	}
}

// frameworkSteps reads the framework's providers and products.
func (in *Inputs) frameworkSteps(ctx context.Context, tx pgx.Tx) []func() error {
	return []func() error{
		func() (err error) {
			in.FrameworkProviders, err = collect(ctx, tx, frameworkProvidersSQL,
				func(r pgx.Rows, v *string) error { return r.Scan(v) })
			return
		},
		func() (err error) {
			in.FrameworkProducts, err = collect(ctx, tx, frameworkProductsSQL,
				func(r pgx.Rows, v *FrameworkProduct) error { return r.Scan(&v.ID, &v.Provider, &v.Name) })
			return
		},
	}
}

// loadSnapshot reads the current snapshot row, if any, and its networks,
// whose rows are read through db afterwards. A build before v4 is
// ErrBeforeV4, read no further.
func (in *Inputs) loadSnapshot(ctx context.Context, db TxBeginner, tx pgx.Tx, lists *Lists) error {
	var s SnapshotRow
	var v4 bool
	err := tx.QueryRow(ctx, snapshotSQL).Scan(&s.BuildID, &s.ContentHash, &s.Payload, &v4)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil
	}
	if err != nil {
		return err
	}
	if !v4 {
		return fmt.Errorf("snapshot %d: %w", s.BuildID, ErrBeforeV4)
	}
	in.Snapshot = &s
	return s.readNetworks(ctx, tx, lists, db)
}
