// Package snap reads the framework's published calculated snapshot (v1 or v2,
// see src/lib/measured-types.ts and proxy-index calculated/calculator.py build()).
//
// Only the fields the website maps are typed. Num fields decode missing keys
// and nulls as NaN, which is what the TypeScript port relies on
// (`number(undefined)` is NaN there). Each product keeps its raw JSON so the
// API can pass the ProductResult through unchanged as Provider.measurement.
//
// A build stored since calculated v8 is a head and its parts (proxy-index
// calculated/parts.py, contract K1), with its daily groups' 24-hour series
// sparse; store reads it back as the single payload of an older build,
// every bucket included, so it decodes here the same way.
package snap

import (
	"encoding/json"
	"time"

	"github.com/Zeus-Development/proxy-index-website/api/internal/jsnum"
	"github.com/Zeus-Development/proxy-index-website/api/internal/model"
)

type Num = jsnum.Num

type Latency struct {
	P50 Num `json:"p50"`
	P90 Num `json:"p90"`
	P95 Num `json:"p95"`
	P99 Num `json:"p99"`
}

func (v *Latency) UnmarshalJSON(d []byte) error {
	type plain Latency
	return jsnum.DecodeNA(d, (*plain)(v))
}

type Metrics struct {
	Completed     Num     `json:"completed"`
	Successes     Num     `json:"successes"`
	Errors        Num     `json:"errors"`
	BodyBytes     Num     `json:"body_bytes"`
	SuccessRate   Num     `json:"success_rate"`
	TimeoutRate   Num     `json:"timeout_rate"`
	ChallengeRate Num     `json:"challenge_rate"`
	BlockRate     Num     `json:"block_rate"`
	Uptime        Num     `json:"uptime"`
	BytesUp       Num     `json:"bytes_up"`
	BytesDown     Num     `json:"bytes_down"`
	LatencyMs     Latency `json:"latency_ms"`
	// Every-request snapshots (framework score-from-all-requests): SuccessRate is
	// the mean of the time-slot success rates and RequestSuccessRate successes ÷
	// requests; LatencyMs is light-load latency (pulses only) and
	// LatencyUnderLoadMs the latency under load (nil when null: no load test).
	RequestSuccessRate Num        `json:"request_success_rate"`
	LatencyUnderLoadMs *Latency   `json:"latency_under_load_ms"`
	UnderLoad          *UnderLoad `json:"under_load"`
	// TimeSlots is present exactly in every-request snapshots (see EveryRequest).
	TimeSlots *TimeSlots `json:"time_slots"`
}

// EveryRequest reports whether metrics come from the framework's every-request
// scoring (time-weighted success, light-load latency_ms, latency under load).
// Older snapshots have no time_slots.
func (m Metrics) EveryRequest() bool { return m.TimeSlots != nil }

type UnderLoad struct {
	Slots         Num     `json:"slots"`
	OwnFactor     Num     `json:"own_factor"`
	CarriedFactor Num     `json:"carried_factor"`
	WithoutFactor Num     `json:"without_factor"`
	Reason        *string `json:"reason"`
}

func (v *UnderLoad) UnmarshalJSON(d []byte) error {
	type plain UnderLoad
	return jsnum.DecodeNA(d, (*plain)(v))
}

type TimeSlots struct {
	Minutes Num `json:"minutes"`
	Success Num `json:"success"`
	Down    Num `json:"down"`
	Latency Num `json:"latency"`
	// LatencyTargets lists the targets whose light-load latency voted in
	// this window (nil in payloads that do not say).
	LatencyTargets      []string `json:"latency_targets"`
	MinLatencySuccesses Num      `json:"min_latency_successes"`
}

func (v *TimeSlots) UnmarshalJSON(d []byte) error {
	type plain TimeSlots
	return jsnum.DecodeNA(d, (*plain)(v))
}

func (v *Metrics) UnmarshalJSON(d []byte) error {
	type plain Metrics
	return jsnum.DecodeNA(d, (*plain)(v))
}

type CountryIPs struct {
	Country     string `json:"country"`
	CountryCode string `json:"country_code"`
	Ips         Num    `json:"ips"`
}

func (v *CountryIPs) UnmarshalJSON(d []byte) error {
	type plain CountryIPs
	return jsnum.DecodeNA(d, (*plain)(v))
}

type AsnIPs struct {
	Asn       Num    `json:"asn"`
	Name      string `json:"name"`
	UsageType string `json:"usage_type"`
	Ips       Num    `json:"ips"`
}

func (v *AsnIPs) UnmarshalJSON(d []byte) error {
	type plain AsnIPs
	return jsnum.DecodeNA(d, (*plain)(v))
}

type Pool struct {
	UniqueIps          Num `json:"unique_ips"`
	ChurnRate          Num `json:"churn_rate"`
	RecurringShare     Num `json:"recurring_share"`
	Countries          Num `json:"countries"`
	Regions            Num `json:"regions"`
	Cities             Num `json:"cities"`
	Asns               Num `json:"asns"`
	DatacenterAsnShare Num `json:"datacenter_asn_share"`
	// GeoAccuracy is, in every-request snapshots (SharesBasis set), the share of
	// exit IPs located in the requested country: the authenticity raw value.
	GeoAccuracy        Num          `json:"geo_accuracy"`
	SharesBasis        *string      `json:"shares_basis"`
	CountriesBreakdown []CountryIPs `json:"countries_breakdown"`
	AsnsBreakdown      []AsnIPs     `json:"asns_breakdown"`
}

func (v *Pool) UnmarshalJSON(d []byte) error {
	type plain Pool
	return jsnum.DecodeNA(d, (*plain)(v))
}

type Component struct {
	Value Num    `json:"value"`
	State string `json:"state"`
	// Inputs is kept raw: an object with "peers" (ready networks of the kind,
	// itself included) in current payloads, a bare number in older ones.
	Inputs json.RawMessage `json:"inputs"`
}

// Peers is the component's inputs.peers, NaN when it has none.
func (c Component) Peers() Num {
	var in struct {
		Peers *float64 `json:"peers"`
	}
	if len(c.Inputs) == 0 || json.Unmarshal(c.Inputs, &in) != nil || in.Peers == nil {
		return jsnum.NaN
	}
	return Num(*in.Peers)
}

// Peers is the most ready networks of the kind any component was scored
// against (itself included); 1 means the network is alone in its kind.
func (s Score) Peers() Num {
	best := jsnum.NaN
	for _, c := range s.Components {
		if p := c.Peers(); p.OK() && (!best.OK() || p > best) {
			best = p
		}
	}
	return best
}

func (v *Component) UnmarshalJSON(d []byte) error {
	type plain Component
	return jsnum.DecodeNA(d, (*plain)(v))
}

// Score keeps the component order of the payload for a stable mapping.
type Score struct {
	Score        Num                  `json:"score"`
	Grade        *string              `json:"grade"`
	Rank         Num                  `json:"rank"`
	PreviousRank Num                  `json:"previous_rank"`
	State        string               `json:"state"`
	Components   map[string]Component `json:"components"`
}

func (v *Score) UnmarshalJSON(d []byte) error {
	type plain Score
	return jsnum.DecodeNA(d, (*plain)(v))
}

// EmptyScore is an unavailable score.
func EmptyScore() Score {
	var s Score
	jsnum.FillNaN(&s)
	return s
}

type TargetResult struct {
	Metrics Metrics `json:"metrics"`
	// Browser is present exactly on browser targets (browser pulse, K-BP6);
	// nil means a network target, as in every older snapshot.
	Browser *BrowserResult `json:"browser"`
}

// BrowserResult is a browser target's targets.<id>.browser: its outcome
// shares, the page-load times of its successful tries, the group target it is
// a source of (nil when none) and, on a group target, its sources in order.
type BrowserResult struct {
	OutcomeRates OutcomeRates `json:"outcome_rates"`
	PageLoadMs   Latency      `json:"page_load_ms"`
	Group        *string      `json:"group"`
	Sources      []string     `json:"sources"`
}

func (v *BrowserResult) UnmarshalJSON(d []byte) error {
	type plain BrowserResult
	return jsnum.DecodeNA(d, (*plain)(v))
}

// OutcomeRates are fractions of the completed tries (all null when none).
type OutcomeRates struct {
	Ok         Num `json:"ok"`
	Challenge  Num `json:"challenge"`
	Block      Num `json:"block"`
	LoginWall  Num `json:"login_wall"`
	Timeout    Num `json:"timeout"`
	ProxyError Num `json:"proxy_error"`
	NoContent  Num `json:"no_content"`
	NoAnswer   Num `json:"no_answer"`
	OtherError Num `json:"other_error"`
}

func (v *OutcomeRates) UnmarshalJSON(d []byte) error {
	type plain OutcomeRates
	return jsnum.DecodeNA(d, (*plain)(v))
}

func (v *TargetResult) UnmarshalJSON(d []byte) error {
	type plain TargetResult
	return jsnum.DecodeNA(d, (*plain)(v))
}

type SeriesRow struct {
	At                 string   `json:"at"`
	Completed          Num      `json:"completed"`
	Successes          Num      `json:"successes"`
	SuccessRate        Num      `json:"success_rate"`
	LatencyMs          Latency  `json:"latency_ms"`
	LatencyUnderLoadMs *Latency `json:"latency_under_load_ms,omitempty"`
	UniqueIps          Num      `json:"unique_ips"`
	Uptime             Num      `json:"uptime"`
}

func (v *SeriesRow) UnmarshalJSON(d []byte) error {
	type plain SeriesRow
	return jsnum.DecodeNA(d, (*plain)(v))
}

type Period struct {
	Start string `json:"start"`
	End   string `json:"end"`
	Hours Num    `json:"hours"`
}

func (v *Period) UnmarshalJSON(d []byte) error {
	type plain Period
	return jsnum.DecodeNA(d, (*plain)(v))
}

type Uptime struct {
	Batches   Num     `json:"batches"`
	Up        Num     `json:"up"`
	Value     Num     `json:"value"`
	DowntimeS Num     `json:"downtime_s"`
	Basis     *string `json:"basis"`
	Countries Num     `json:"countries"`
}

func (v *Uptime) UnmarshalJSON(d []byte) error {
	type plain Uptime
	return jsnum.DecodeNA(d, (*plain)(v))
}

type BreakdownRow struct {
	CountryCode *string `json:"country_code"`
	Country     *string `json:"country"`
	// Region and city rows label their place; ASN rows their network.
	RegionCode  *string `json:"region_code"`
	Region      *string `json:"region"`
	City        *string `json:"city"`
	Asn         Num     `json:"asn"`
	Name        *string `json:"name"`
	Ips         Num     `json:"ips"`
	Completed   Num     `json:"completed"`
	Successes   Num     `json:"successes"`
	SuccessRate Num     `json:"success_rate"`
	P50         Num     `json:"p50"`
	P90         Num     `json:"p90"`
	P99         Num     `json:"p99"`
}

func (v *BreakdownRow) UnmarshalJSON(d []byte) error {
	type plain BreakdownRow
	return jsnum.DecodeNA(d, (*plain)(v))
}

type WindowOverlap struct {
	ExclusiveShare   Num `json:"exclusive_share"`
	SharedShare      Num `json:"shared_share"`
	ComparedProducts Num `json:"compared_products"`
}

func (v *WindowOverlap) UnmarshalJSON(d []byte) error {
	type plain WindowOverlap
	return jsnum.DecodeNA(d, (*plain)(v))
}

type Window struct {
	Period       Period                    `json:"period"`
	Readiness    string                    `json:"readiness"`
	Metrics      Metrics                   `json:"metrics"`
	Pool         Pool                      `json:"pool"`
	Targets      map[string]TargetResult   `json:"targets"`
	Scores       map[string]Score          `json:"scores"`
	Series       []SeriesRow               `json:"series"`
	SuccessDelta Num                       `json:"success_delta"`
	Overlap      WindowOverlap             `json:"overlap"`
	Uptime       *Uptime                   `json:"uptime"`
	Breakdowns   map[string][]BreakdownRow `json:"breakdowns"`
	// BreakdownEntries is each capped list's full row count (absent when
	// nothing was capped).
	BreakdownEntries map[string]int `json:"breakdown_entries"`
	// Estimated is the window's released hourly estimates (nil when it has
	// none: census-pulse groups, or no hour released inside the window).
	Estimated *Estimated `json:"estimated"`
}

// Estimated is a window's `estimated` block (proxy-index
// estimates/window.py): the unique IPs the hour formula gives the window and
// each released UTC hour, and BreakdownScale, the estimated over the measured
// unique IPs (NaN when nothing was measured). Only the figures the API maps
// are typed: a daily group's cohort is one requested country, so its
// per-country figure (countries[]) is its window figure.
type Estimated struct {
	UniqueIps      EstimatedValue  `json:"unique_ips"`
	Series         []EstimatedHour `json:"series"`
	BreakdownScale Num             `json:"breakdown_scale"`
}

func (v *Estimated) UnmarshalJSON(d []byte) error {
	type plain Estimated
	return jsnum.DecodeNA(d, (*plain)(v))
}

type EstimatedValue struct {
	Value Num `json:"value"`
}

func (v *EstimatedValue) UnmarshalJSON(d []byte) error {
	type plain EstimatedValue
	return jsnum.DecodeNA(d, (*plain)(v))
}

// EstimatedHour is one released hour: Start is its UTC hour.
type EstimatedHour struct {
	Start     string `json:"start"`
	UniqueIps Num    `json:"unique_ips"`
}

func (v *EstimatedHour) UnmarshalJSON(d []byte) error {
	type plain EstimatedHour
	return jsnum.DecodeNA(d, (*plain)(v))
}

func (v *Window) UnmarshalJSON(d []byte) error {
	type plain Window
	return jsnum.DecodeNA(d, (*plain)(v))
}

// EmptyWindow is a window with nothing measured.
func EmptyWindow() Window {
	var w Window
	jsnum.FillNaN(&w)
	return w
}

type Census struct {
	Country    string  `json:"country"`
	Region     *string `json:"region"`
	FinishedAt *string `json:"finished_at"`
	Requests   Num     `json:"requests"`
	Successes  Num     `json:"successes"`
	UniqueIps  Num     `json:"unique_ips"`
}

func (v *Census) UnmarshalJSON(d []byte) error {
	type plain Census
	return jsnum.DecodeNA(d, (*plain)(v))
}

// Outage is one span a product was down in one country (snapshot v2
// `outages`, the newest 50 overlapping the 30d window). EndedAt is nil while
// it is still open; DurationS then runs to the snapshot's as_of.
type Outage struct {
	Country   string  `json:"country"`
	Region    *string `json:"region"`
	Source    string  `json:"source"`
	StartedAt string  `json:"started_at"`
	EndedAt   *string `json:"ended_at"`
	DurationS Num     `json:"duration_s"`
}

func (v *Outage) UnmarshalJSON(d []byte) error {
	type plain Outage
	return jsnum.DecodeNA(d, (*plain)(v))
}

// Pricing is a product's effective list prices, USD per GB of the tested pool:
// LowestPerGb is the cheapest plan's rate (it scores value), EntryPerGb the
// smallest plan's.
type Pricing struct {
	Currency      *string  `json:"currency"`
	LowestPerGb   Num      `json:"lowest_per_gb"`
	EntryPerGb    Num      `json:"entry_per_gb"`
	EffectiveFrom *string  `json:"effective_from"`
	ValidUntil    *string  `json:"valid_until"`
	Sources       []string `json:"sources"`
}

func (v *Pricing) UnmarshalJSON(d []byte) error {
	type plain Pricing
	return jsnum.DecodeNA(d, (*plain)(v))
}

type Product struct {
	ID        string            `json:"id"`
	ProductID Num               `json:"product_id"`
	Provider  string            `json:"provider"`
	Product   string            `json:"product"`
	Name      string            `json:"name"`
	Kind      string            `json:"kind"`
	Cohort    string            `json:"cohort"`
	Criteria  Criteria          `json:"criteria"`
	Windows   map[string]Window `json:"windows"`
	Census    []Census          `json:"census"`
	// Pricing is the every-request snapshot's list prices in effect (nil before).
	Pricing *Pricing `json:"pricing"`
	// Outages is nil for a snapshot that does not publish them (v1), and empty
	// when it does and the product had none.
	Outages []Outage        `json:"outages"`
	Raw     json.RawMessage `json:"-"`
}

func (v *Product) UnmarshalJSON(d []byte) error {
	type plain Product
	if err := jsnum.DecodeNA(d, (*plain)(v)); err != nil {
		return err
	}
	v.Raw = append(json.RawMessage(nil), d...)
	return nil
}

// Criteria is what makes a product group's cohort; Country is the requested
// country of a daily test's cohort (nil for census/pulse cohorts).
type Criteria struct {
	Country *string `json:"country"`
}

// ProxyKind is a product's proxy category: its kind, residential when the
// kind is not a proxy category.
func ProxyKind(kind string) string {
	if model.IsProxyKind(kind) {
		return kind
	}
	return "residential"
}

// ByKind is, per proxy category, the product with the most 30-day completed
// requests (the first on a tie), with the categories in first-seen order: the
// group a provider serves for each kind.
func ByKind(products []*Product) (map[string]*Product, []string) {
	byKind := map[string]*Product{}
	kinds := []string{}
	for _, p := range products {
		k := ProxyKind(p.Kind)
		cur, ok := byKind[k]
		if !ok {
			kinds = append(kinds, k)
		}
		if !ok || p.Window("30d").Metrics.Completed > cur.Window("30d").Metrics.Completed {
			byKind[k] = p
		}
	}
	return byKind, kinds
}

// Window returns a window or an empty one.
func (p *Product) Window(label string) Window {
	if w, ok := p.Windows[label]; ok {
		return w
	}
	return EmptyWindow()
}

type OverlapRow struct {
	Cohort    string `json:"cohort"`
	Window    string `json:"window"`
	ProductA  Num    `json:"product_a"`
	ProductB  Num    `json:"product_b"`
	SharedIps Num    `json:"shared_ips"`
	IpsA      Num    `json:"ips_a"`
	IpsB      Num    `json:"ips_b"`
}

func (v *OverlapRow) UnmarshalJSON(d []byte) error {
	type plain OverlapRow
	return jsnum.DecodeNA(d, (*plain)(v))
}

// FleetRow is one `fleet` entry: a cohort's product groups together in one
// window or, with cohort "*" (AllCohorts), every product group of the build,
// each exit IP counted once however many networks or cohorts saw it. Only the
// "*" entries carry a series (5-minute buckets for 24h, days otherwise).
type FleetRow struct {
	Cohort    string      `json:"cohort"`
	Window    string      `json:"window"`
	Products  Num         `json:"products"`
	Providers Num         `json:"providers"`
	Metrics   Metrics     `json:"metrics"`
	UniqueIps Num         `json:"unique_ips"`
	Series    []SeriesRow `json:"series"`
}

func (v *FleetRow) UnmarshalJSON(d []byte) error {
	type plain FleetRow
	return jsnum.DecodeNA(d, (*plain)(v))
}

// AllCohorts is the fleet cohort of every product group together.
const AllCohorts = "*"

// FleetAll is the window's every-group fleet entry, nil when the snapshot
// does not publish one (builds before it did).
func (v *Snapshot) FleetAll(window string) *FleetRow {
	for i := range v.Fleet {
		if f := &v.Fleet[i]; f.Cohort == AllCohorts && f.Window == window {
			return f
		}
	}
	return nil
}

type Methodology struct {
	ID       string `json:"id"`
	Settings struct {
		Weights map[string]float64 `json:"weights"`
		// TargetGroups is each group target's sources, in order (browser
		// pulse methodology [target_groups]; nil before it).
		TargetGroups map[string][]string `json:"target_groups"`
	} `json:"settings"`
}

type Snapshot struct {
	SchemaVersion    Num          `json:"schema_version"`
	SnapshotID       string       `json:"snapshot_id"`
	ImplementationID *string      `json:"implementation_id"`
	AsOf             string       `json:"as_of"`
	GeneratedAt      string       `json:"generated_at"`
	Readiness        string       `json:"readiness"`
	RefreshSeconds   Num          `json:"refresh_seconds"`
	Methodology      Methodology  `json:"methodology"`
	Products         []Product    `json:"products"`
	Fleet            []FleetRow   `json:"fleet"`
	Overlap          []OverlapRow `json:"overlap"`
}

func (v *Snapshot) UnmarshalJSON(d []byte) error {
	type plain Snapshot
	return jsnum.DecodeNA(d, (*plain)(v))
}

// ParseTime reads the ISO stamps the framework writes (Python isoformat or
// JavaScript toISOString). The zero time means unparseable, like Date.parse's NaN.
func ParseTime(s string) (time.Time, bool) {
	for _, layout := range []string{time.RFC3339Nano, "2006-01-02T15:04:05.999999999Z0700", "2006-01-02T15:04:05.999999999", "2006-01-02 15:04:05.999999999Z07:00"} {
		if t, err := time.Parse(layout, s); err == nil {
			return t, true
		}
	}
	return time.Time{}, false
}

// Ms is Date.parse in milliseconds, NaN when unparseable.
func Ms(s string) float64 {
	t, ok := ParseTime(s)
	if !ok {
		return jsnum.NaN.F()
	}
	return float64(t.UnixMilli())
}
