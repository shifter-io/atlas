package store

import (
	"context"
	"encoding/json"
	"fmt"
	"strconv"
	"time"

	"github.com/jackc/pgx/v5"
)

// Fingerprint is the cheap change detector polled every refresh interval.
type Fingerprint struct {
	BuildID *int64 `json:"build_id"`
	// BuildCreatedAt is the published build's created_at, part of the
	// dataset file's marker (server.datasetMarker).
	BuildCreatedAt *time.Time `json:"build_created_at"`
	MaxEventID     *int64     `json:"max_event_id"`
	SiteRevision   int64      `json:"site_revision"`
	SiteUpdatedAt  *time.Time `json:"site_updated_at"`
	// SiteTables is the site tables that exist (comma separated), so a site
	// schema created or changed under a running server is picked up.
	SiteTables    string  `json:"site_tables"`
	Products      int64   `json:"framework_products"`
	TargetsDigest *string `json:"framework_targets_digest"`
}

// Key is a comparable string form.
func (f Fingerprint) Key() string {
	b, _ := json.Marshal(f)
	return string(b)
}

// DataKey is Key without the newest event id: everything a full build reads
// except the events. When it changes, the cache is rebuilt.
func (f Fingerprint) DataKey() string {
	f.MaxEventID = nil
	return f.Key()
}

// EventsKey is the fingerprint's events part, the newest event id ("" while
// test_events is empty). When only it changes, only the events answers are
// refreshed (LoadEvents).
func (f Fingerprint) EventsKey() string {
	if f.MaxEventID == nil {
		return ""
	}
	return strconv.FormatInt(*f.MaxEventID, 10)
}

const fingerprintSQL = `SELECT
 (SELECT build_id FROM public.current_calculation WHERE singleton),
 (SELECT b.created_at FROM public.calculation_builds b JOIN public.current_calculation c USING (build_id) WHERE c.singleton),
 (SELECT max(event_id) FROM public.test_events),
 %s,
 %s,
 (SELECT count(*) FROM public.products),
 (SELECT md5(string_agg(target_id || '|' || coalesce(label,'') || '|' || coalesce(pass_condition,'') || '|' || sort_order || '|' || engine, ',' ORDER BY target_id)) FROM public.targets)`

// ReadFingerprint runs the change-detection queries: the site layout, then
// one query over the framework tables and the site tables that exist.
func ReadFingerprint(ctx context.Context, q Querier) (Fingerprint, error) {
	l, err := ReadSiteLayout(ctx, q)
	if err != nil {
		return Fingerprint{}, err
	}
	return readFingerprint(ctx, q, l)
}

func readFingerprint(ctx context.Context, q Querier, l SiteLayout) (Fingerprint, error) {
	revision, updated, tables := siteFingerprintSQL(l)
	f := Fingerprint{SiteTables: tables}
	err := q.QueryRow(ctx, fmt.Sprintf(fingerprintSQL, revision, updated)).Scan(&f.BuildID, &f.BuildCreatedAt, &f.MaxEventID, &f.SiteRevision, &f.SiteUpdatedAt, &f.Products, &f.TargetsDigest)
	return f, err
}

type TargetRow struct {
	ID            string
	Label         *string
	Category      *string
	PassCondition *string
	Difficulty    *int16
	SortOrder     int32
}

type FrameworkTarget struct {
	ID            string
	Label         *string
	PassCondition *string
	SortOrder     int32
	// Engine is "http" or "browser" (public.targets.engine, NOT NULL since
	// framework schema v1).
	Engine string
}

type ScopeRow struct {
	Kind                                                   string
	Label, Summary, Cadence                                *string
	AllTargets                                             bool
	TargetIDs                                              []string
	LiveIPSampling, PoolTracking, OverlapAudit, Stickiness *bool
	GeoCoverage, Rationale                                 *string
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
}

type ProviderRow struct {
	Slug                           string
	Name                           string
	Tagline                        *string
	Kinds                          []string
	PrimaryKind, MeasuredProduct   *string
	HQ                             *string
	Founded                        *int32
	Claim, Verdict                 *string
	Strengths, Weaknesses, BestFor []string // nil when the column is NULL
	Website                        *string
	FrameworkProvider              *string
	Listed                         bool
}

type ProductRow struct {
	ProviderSlug     string
	Kind             string
	Name             *string
	Measured         bool
	URL, PriceLabel  *string
	Note             *string
	FrameworkProduct *string
}

type PricingRow struct {
	ProviderSlug, Category                   string
	Model                                    *string
	PricePerGb, PricePerGbAtScale, MinCommit *float64
	Trial                                    *string
	CostPer1kSuccess, AvgPayloadKb           *float64
	CostBasis                                *string
	HeadlinePriceRank, ValueRank             *int32
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
}

type ChangeRow struct {
	ID, Kind         string
	ProviderSlug     *string
	Headline, Detail string
	Delta            *float64
	Unit, Severity   string
	OccurredAt       time.Time
}

type TapeRow struct {
	ID, Kind, ProviderSlug, Product, Title, Detail string
	Signal                                         int16
	Value                                          string
	OccurredAt                                     time.Time
}

type FrameworkProduct struct {
	ID       int32
	Provider string
	Name     string
}

type SnapshotRow struct {
	BuildID     int64
	ContentHash string
	// Payload is the v4 head (parts.go).
	Payload []byte
	full    []byte // Payload with its networks as product groups (network.go)
	// Places is each target-free window's typed list (lists.go), which only
	// a build before v4 had; nil.
	Places map[PlaceWindow]*List
	// Networks is each network window's compact list (network.go) of a v4
	// build; nil before v4.
	Networks map[PlaceWindow]*NetList
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
	// Notable holds the newest non-routine events older than Events (at most
	// NotableWindow), so routine bursts never push them out of memory;
	// NotableComplete is true when it holds every such row.
	Notable         []Event
	NotableComplete bool
	// SiteGaps is one line per site table read as empty because the table or
	// a column it reads is missing (SiteLayout); none on a migrated database.
	SiteGaps []string
}

// loadEvents reads the newest EventWindow events and, past them, the newest
// NotableWindow non-routine ones.
func (in *Inputs) loadEvents(ctx context.Context, tx pgx.Tx) (err error) {
	in.Events, err = collect(ctx, tx, eventsNewestSQL, ScanEvent, EventWindow+1)
	if err != nil {
		return err
	}
	in.EventsComplete, in.NotableComplete = len(in.Events) <= EventWindow, true
	if in.EventsComplete {
		return nil
	}
	in.Events = in.Events[:EventWindow]
	last := in.Events[EventWindow-1]
	in.Notable, err = collect(ctx, tx, eventsNotableSQL, ScanEvent, last.At, last.ID, NotableWindow+1)
	if len(in.Notable) > NotableWindow {
		in.Notable, in.NotableComplete = in.Notable[:NotableWindow], false
	}
	return err
}

// eventsNewestSQL reads the newest $1 events; eventsNotableSQL the newest $3
// non-routine ones older than the event at $1 with id $2.
const (
	eventsOrder      = ` ORDER BY e.at DESC, e.event_id DESC LIMIT `
	eventsNewestSQL  = `SELECT ` + EventColumns + eventsOrder + `$1`
	eventsNotableSQL = `SELECT ` + EventColumns + ` WHERE NOT ` + routineSQL +
		` AND e.at <= $1 AND (e.at, e.event_id) < ($1, $2)` + eventsOrder + `$3`
)

// EventWindow is how many of the newest events are held in memory.
const EventWindow = 2000

// NotableWindow is how many non-routine events older than the window are held.
const NotableWindow = 500

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
