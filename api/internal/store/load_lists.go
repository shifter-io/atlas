package store

import (
	"context"
	"errors"
	"fmt"

	"github.com/jackc/pgx/v5"
)

// The optional site reads; a read whose table or columns are missing is empty.
var (
	readTargets   = siteRead{"targets", []string{"target_id", "label", "category", "pass_condition", "difficulty", "sort_order"}, "sort_order, target_id"}
	readScopes    = siteRead{"scopes", []string{"product_kind", "label", "summary", "cadence", "all_targets", "target_ids", "live_ip_sampling", "pool_tracking", "overlap_audit", "stickiness_test", "geo_coverage", "rationale"}, "sort_order, product_kind"}
	readWeights   = siteRead{"score_weights", []string{"component", "weight"}, "component"}
	readProfiles  = siteRead{"ranking_profiles", []string{"profile_id", "label", "description", "weights"}, "sort_order, profile_id"}
	readProviders = siteRead{"providers", []string{"slug", "name", "tagline", "kinds", "primary_kind", "measured_product", "hq", "founded", "claim", "verdict",
		"strengths", "weaknesses", "best_for", "website", "framework_provider", "listed"}, "sort_order, slug"}
	readProducts = siteRead{"provider_products", []string{"provider_slug", "product_kind", "name", "measured", "url", "price_label", "note", "framework_product"}, "provider_slug, sort_order, product_kind"}
	readPricing  = siteRead{"pricing", []string{"provider_slug", "category", "model", "price_per_gb", "price_per_gb_at_scale", "min_commit", "trial",
		"cost_per_1k_success", "avg_payload_kb", "cost_basis", "headline_price_rank", "value_rank"}, "provider_slug, category"}
	readPlans = siteRead{"pricing_plans", []string{"provider_slug", "category", "position", "name", "price", "gb", "ips", "note", "recommended", "discount_pct", "catalog"}, "provider_slug, category, position"}
	readTrust = siteRead{"trust_signals", []string{"provider_slug", "category", "residential_authenticity", "datacenter_asn_share", "claim_gap", "blacklist_hit_rate",
		"stickiness_hold_rate", "stickiness_advertised", "claimed_pool_size", "leak_checks", "kyc_verified", "consent_disclosed"}, "provider_slug, category"}
	readReviews = siteRead{"reviews", []string{"review_id", "provider_slug", "author", "role", "rating", "title", "body", "product_kind", "use_case", "monthly_spend",
		"months_used", "verification", "invoice_month", "linked_run_id", "helpful_count", "provider_reply", "posted_at"}, "posted_at DESC, position NULLS LAST, review_id"}
	readRuns = siteRead{"community_runs", []string{"run_id", "provider_slug", "submitted_by", "region", "product_kind", "plan_tier", "collector_version", "status",
		"rejection_reason", "requests", "success_rate", "latency_p50", "unique_ips", "overlap_share", "delta_success_pts", "delta_latency_pct", "targets",
		"success_series", "latency_series", "submitted_at"}, "submitted_at DESC, position NULLS LAST, run_id"}
	readChangelog = siteRead{"changelog", []string{"change_id", "kind", "provider_slug", "headline", "detail", "delta", "unit", "severity", "occurred_at"}, "occurred_at DESC, position NULLS LAST, change_id"}
	readTape      = siteRead{"tape_events", []string{"tape_id", "kind", "provider_slug", "product", "title", "detail", "signal", "value", "occurred_at"}, "occurred_at DESC, position NULLS LAST, tape_id"}

	siteReads = []siteRead{readTargets, readScopes, readWeights, readProfiles, readProviders, readProducts, readPricing, readPlans, readTrust,
		readReviews, readRuns, readChangelog, readTape}
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

// TxBeginner starts the read-only transactions of Load and LoadEvents (a
// pgxpool.Pool or a pgx.Conn).
type TxBeginner interface {
	BeginTx(ctx context.Context, opts pgx.TxOptions) (pgx.Tx, error)
}

// LoadEvents reads only what an events-only change refreshes, in one
// REPEATABLE READ read-only transaction: the newest event id
// (Fingerprint.MaxEventID, the fingerprint's events part) and the events
// exactly as Load reads them (Events, Notable and their completeness). Every
// other field is empty.
func LoadEvents(ctx context.Context, db TxBeginner) (*Inputs, error) {
	tx, err := db.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.RepeatableRead, AccessMode: pgx.ReadOnly})
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx) //nolint:errcheck
	in := &Inputs{}
	if err := tx.QueryRow(ctx, `SELECT max(event_id) FROM public.test_events`).Scan(&in.Fingerprint.MaxEventID); err != nil {
		return nil, fmt.Errorf("newest event: %w", err)
	}
	if err := in.loadEvents(ctx, tx); err != nil {
		return nil, fmt.Errorf("events: %w", err)
	}
	return in, nil
}

// Load reads every input in one consistent read-only snapshot, every list
// afresh (LoadLists).
func Load(ctx context.Context, db TxBeginner) (*Inputs, error) { return LoadLists(ctx, db, nil) }

// afterSnapshot, when set, runs once the current snapshot row is read, and
// lockBreakdowns off skips the lock below: tests of the concurrent reads.
var (
	afterSnapshot  func()
	lockBreakdowns = true
)

// LoadLists reads every input in one consistent read-only snapshot, holding
// the typed lists in lists between calls (nil: none held). Only the
// framework schema is required; site tables that are missing read as empty
// (Inputs.SiteGaps names them). A v4 build's network lists read their rows
// through db when a build or a request needs them: db stays open while the
// Inputs, or a build made from them, are used.
//
// Before K2 a build's typed rows are a calculated_breakdowns partition, which
// a publication can drop: the snapshot would still see the build as current
// and find no rows. The read therefore locks the calculated_breakdowns parent
// (ACCESS SHARE, ONLY: no partition rights needed) before its snapshot is
// taken; dropping a partition locks its parent exclusively, so it waits. K2 deletes
// lists instead, and the snapshot keeps seeing them.
func LoadLists(ctx context.Context, db TxBeginner, lists *Lists) (*Inputs, error) {
	partitioned, err := hasBreakdowns(ctx, db)
	if err != nil {
		return nil, err
	}
	tx, err := db.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.RepeatableRead, AccessMode: pgx.ReadOnly})
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx) //nolint:errcheck
	if partitioned && lockBreakdowns {
		if _, err := tx.Exec(ctx, `LOCK TABLE ONLY public.calculated_breakdowns IN ACCESS SHARE MODE`); err != nil {
			return nil, fmt.Errorf("lock calculated_breakdowns: %w", err)
		}
	}
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
		func() error { return in.loadEvents(ctx, tx) },
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
	return l, tx.QueryRow(ctx, "SELECT now()").Scan(&in.ReadAt)
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
			in.FrameworkTargets, err = collect(ctx, tx, `SELECT target_id, label, pass_condition, sort_order, engine FROM public.targets ORDER BY sort_order, target_id`,
				func(r pgx.Rows, v *FrameworkTarget) error {
					return r.Scan(&v.ID, &v.Label, &v.PassCondition, &v.SortOrder, &v.Engine)
				})
			return
		},
		func() (err error) {
			in.Scopes, err = readSite(ctx, tx, l, readScopes, func(r pgx.Rows, v *ScopeRow) error {
				return r.Scan(&v.Kind, &v.Label, &v.Summary, &v.Cadence, &v.AllTargets, &v.TargetIDs, &v.LiveIPSampling, &v.PoolTracking, &v.OverlapAudit, &v.Stickiness, &v.GeoCoverage, &v.Rationale)
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

// providerSteps reads the providers with their products, pricing, plans and
// trust signals.
func (in *Inputs) providerSteps(ctx context.Context, tx pgx.Tx, l SiteLayout) []func() error {
	return []func() error{
		func() (err error) {
			in.Providers, err = readSite(ctx, tx, l, readProviders, func(r pgx.Rows, v *ProviderRow) error {
				return r.Scan(&v.Slug, &v.Name, &v.Tagline, &v.Kinds, &v.PrimaryKind, &v.MeasuredProduct, &v.HQ, &v.Founded, &v.Claim, &v.Verdict, &v.Strengths, &v.Weaknesses, &v.BestFor, &v.Website, &v.FrameworkProvider, &v.Listed)
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
				return r.Scan(&v.ProviderSlug, &v.Category, &v.Model, &v.PricePerGb, &v.PricePerGbAtScale, &v.MinCommit, &v.Trial, &v.CostPer1kSuccess, &v.AvgPayloadKb, &v.CostBasis, &v.HeadlinePriceRank, &v.ValueRank)
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
				return r.Scan(&v.ID, &v.ProviderSlug, &v.Author, &v.Role, &v.Rating, &v.Title, &v.Body, &v.ProductKind, &v.UseCase, &v.Spend, &v.MonthsUsed, &v.Verification, &v.InvoiceMonth, &v.LinkedRunID, &v.HelpfulCount, &v.ProviderReply, &v.PostedAt)
			})
			return
		},
		func() (err error) {
			in.CommunityRuns, err = readSite(ctx, tx, l, readRuns, func(r pgx.Rows, v *CommunityRunRow) error {
				return r.Scan(&v.ID, &v.ProviderSlug, &v.SubmittedBy, &v.Region, &v.ProductKind, &v.PlanTier, &v.CollectorVersion, &v.Status, &v.RejectionReason, &v.Requests, &v.SuccessRate, &v.LatencyP50, &v.UniqueIps, &v.OverlapShare, &v.DeltaSuccessPts, &v.DeltaLatencyPct, &v.Targets, &v.SuccessSeries, &v.LatencySeries, &v.SubmittedAt)
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
			in.FrameworkProviders, err = collect(ctx, tx, `SELECT id FROM public.providers ORDER BY id`,
				func(r pgx.Rows, v *string) error { return r.Scan(v) })
			return
		},
		func() (err error) {
			in.FrameworkProducts, err = collect(ctx, tx, `SELECT product_id, provider, name FROM public.products ORDER BY provider, name`,
				func(r pgx.Rows, v *FrameworkProduct) error { return r.Scan(&v.ID, &v.Provider, &v.Name) })
			return
		},
	}
}

// loadSnapshot reads the current snapshot row, if any, and its parts,
// networks (whose rows are read through db afterwards) or typed lists.
func (in *Inputs) loadSnapshot(ctx context.Context, db TxBeginner, tx pgx.Tx, lists *Lists) error {
	var s SnapshotRow
	var parts *int
	var v4 bool
	err := tx.QueryRow(ctx, snapshotSQL).Scan(&s.BuildID, &s.ContentHash, &s.Payload, &parts, &v4)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil
	}
	if err != nil {
		return err
	}
	if parts != nil {
		if err := s.readParts(ctx, tx, *parts); err != nil {
			return err
		}
	}
	in.Snapshot = &s
	if afterSnapshot != nil {
		afterSnapshot()
	}
	if v4 {
		return s.readNetworks(ctx, tx, lists, db)
	}
	lists.dropNetworks()
	return s.readLists(ctx, tx, lists)
}

// hasBreakdowns reports whether the database keeps typed rows in
// calculated_breakdowns (before K2), in a transaction of its own: LoadLists
// must lock the table before its snapshot.
func hasBreakdowns(ctx context.Context, db TxBeginner) (bool, error) {
	tx, err := db.BeginTx(ctx, pgx.TxOptions{AccessMode: pgx.ReadOnly})
	if err != nil {
		return false, err
	}
	defer tx.Rollback(ctx) //nolint:errcheck
	var ok bool
	err = tx.QueryRow(ctx, `SELECT to_regclass('public.calculated_breakdowns') IS NOT NULL`).Scan(&ok)
	return ok, err
}
