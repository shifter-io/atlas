// Package snap reads the framework's published calculated snapshot (v1 to v4,
// see src/lib/measured-types.ts and atlas calculated/calculator.py build()).
//
// Only the fields the website maps are typed. Num fields decode missing keys
// and nulls as NaN, which is what the TypeScript port relies on
// (`number(undefined)` is NaN there). Each product keeps its raw JSON so the
// API can pass the ProductResult through unchanged as Provider.measurement.
//
// A build stored since calculated v8 is a head and its parts (atlas
// calculated/parts.py, contract K1), with its daily groups' 24-hour series
// sparse; store reads it back as the single payload of an older build,
// every bucket included, so it decodes here the same way. A v4 build
// (contract K9) publishes one network summary per (provider, kind) in the
// head; store gives each as one product group, so it decodes here as a
// payload whose every network has one group.
package snap

import (
	"encoding/json"
	"time"

	"github.com/Zeus-Development/atlas/api/internal/jsnum"
	"github.com/Zeus-Development/atlas/api/internal/model"
)

type Num = jsnum.Num

// Latency is a latency object in milliseconds. Mean is set on the head's
// (window metrics) latency only, and is NaN when the snapshot predates it.
type Latency struct {
	Mean Num `json:"mean"`
	P50  Num `json:"p50"`
	P90  Num `json:"p90"`
	P95  Num `json:"p95"`
	P99  Num `json:"p99"`
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
	// LatencyReason is why LatencyMs is null (nil when it is not, or the
	// payload does not say).
	LatencyReason *string `json:"latency_reason"`
	// Every-request snapshots (framework score-from-all-requests): SuccessRate is
	// the mean of the time-slot success rates and RequestSuccessRate successes ÷
	// requests; LatencyMs is the latency of normal requests (the hourly pulses,
	// concurrency 25) and LatencyUnderLoadMs the latency under load (the daily
	// tests at load concurrency; nil when null: no load test).
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
	Slots  Num     `json:"slots"`
	Reason *string `json:"reason"`
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
	// ClassificationCoverage is the share of UniqueIps with a usage type in
	// the IP database: DatacenterAsnShare's denominator ÷ UniqueIps.
	ClassificationCoverage Num `json:"classification_coverage"`
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

// Pricing is a product's effective list prices, USD per GB of the tested pool:
// LowestPerGb is the cheapest plan's rate, EntryPerGb the smallest plan's.
// BasisPerGb is the rate every cost uses (COST-BASIS-200GB-1): the first plan
// of 200 GB or more, else the largest plan, of BasisPlanGb GB, as PriceBasis
// says ("first_200gb_plus" or "largest_below_200gb"). Older snapshots
// without them decode to NaN and nil.
type Pricing struct {
	Currency      *string  `json:"currency"`
	LowestPerGb   Num      `json:"lowest_per_gb"`
	EntryPerGb    Num      `json:"entry_per_gb"`
	BasisPerGb    Num      `json:"basis_per_gb"`
	BasisPlanGb   Num      `json:"basis_plan_gb"`
	PriceBasis    *string  `json:"price_basis"`
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
	Outages []Outage `json:"outages"`
	// Days is a v4 network's days (DAY-SCORE-1): nil on a product group, or
	// in a snapshot from before them.
	Days []Day `json:"days"`
	// Scoring is the group's scoring settings; only the pulse interval is read.
	Scoring Scoring         `json:"scoring"`
	Raw     json.RawMessage `json:"-"`
}

// Scoring is a product group's scoring settings: Slots.PulseIntervalMinutes
// is the cohort's pulse interval (null without one).
type Scoring struct {
	Slots struct {
		PulseIntervalMinutes Num `json:"pulse_interval_minutes"`
	} `json:"slots"`
}

// Day is one day of a network's 30d window, a day apart from its start as its
// series points (the last is the 24h window's span): the day's overall success
// score over every target, scored as a window's success_overall without its
// weights; nil on a day with no target.
type Day struct {
	Start          string          `json:"start"`
	SuccessOverall *SuccessOverall `json:"success_overall"`
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
// country of a daily test's cohort (nil for census/pulse cohorts). A v4
// network entry has a null Country, its groups' requested countries
// (sorted, upper case) and how many groups it summarizes.
type Criteria struct {
	Country            *string  `json:"country"`
	RequestedCountries []string `json:"requested_countries"`
	Groups             Num      `json:"groups"`
}

func (v *Criteria) UnmarshalJSON(d []byte) error {
	type plain Criteria
	return jsnum.DecodeNA(d, (*plain)(v))
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

type Methodology struct {
	ID       string `json:"id"`
	Settings struct {
		Weights map[string]float64 `json:"weights"`
		// TargetGroups is each group target's sources, in order (browser
		// pulse methodology [target_groups]; nil before it).
		TargetGroups map[string][]string `json:"target_groups"`
		// Grades is each grade's lowest score (methodology [grades], such
		// as "A+": 88); nil when the snapshot does not publish them.
		Grades map[string]float64 `json:"grades"`
		// BrowserDifficulty is each browser target's difficulty, 1 to 5, its
		// weight in the overall browser score ([browser_difficulty]; nil before it).
		BrowserDifficulty map[string]float64 `json:"browser_difficulty"`
		// Windows is each window's length in hours (methodology windows,
		// such as [24, 168, 720]).
		Windows []float64 `json:"windows"`
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
