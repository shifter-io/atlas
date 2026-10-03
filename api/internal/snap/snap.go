// Package snap reads the framework's published calculated snapshot (v1 or v2,
// see src/lib/measured-types.ts and milf calculated/calculator.py build()).
//
// Only the fields the website maps are typed. Num fields decode missing keys
// and nulls as NaN, which is what the TypeScript port relies on
// (`number(undefined)` is NaN there). Each product keeps its raw JSON so the
// API can pass the ProductResult through unchanged as Provider.measurement.
package snap

import (
	"encoding/json"
	"time"

	"github.com/ice-zeus/milf-website/api/internal/jsnum"
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
	Minutes             Num `json:"minutes"`
	Success             Num `json:"success"`
	Down                Num `json:"down"`
	Latency             Num `json:"latency"`
	MinLatencySuccesses Num `json:"min_latency_successes"`
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
}

func (v *TargetResult) UnmarshalJSON(d []byte) error {
	type plain TargetResult
	return jsnum.DecodeNA(d, (*plain)(v))
}

type SeriesRow struct {
	At          string  `json:"at"`
	Completed   Num     `json:"completed"`
	Successes   Num     `json:"successes"`
	SuccessRate Num     `json:"success_rate"`
	LatencyMs   Latency `json:"latency_ms"`
	UniqueIps   Num     `json:"unique_ips"`
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

type Methodology struct {
	ID       string `json:"id"`
	Settings struct {
		Weights map[string]float64 `json:"weights"`
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
