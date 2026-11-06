// Package model is the website's Dataset contract (src/lib/types.ts) in Go.
//
// Every field is always written. A value the API does not have is JSON null
// (Num NaN, nil *string, nil *bool) and its path is listed in the owning
// entity's `na` array. Slices are never nil, so an empty list is [] rather than
// null; when an empty list means "not collected" rather than "none", the
// builder lists it in `na` explicitly.
package model

import (
	"bytes"
	"encoding/json"

	"github.com/Zeus-Development/proxy-index/api/internal/jsnum"
)

type Num = jsnum.Num

// Str and Bool are nullable scalars.
type (
	Str  = *string
	Bool = *bool
)

func S(s string) Str { return &s }
func B(b bool) Bool  { return &b }

// SV returns the string or "".
func SV(s Str) string {
	if s == nil {
		return ""
	}
	return *s
}

// SourceMeasured is the source of every provider and record (a target with
// only a catalog row is "catalog"); the demo fixtures' "demo" is gone.
const SourceMeasured = "measured"

var ProxyKinds = []string{"residential", "datacenter", "mobile", "isp"}

var ProductKinds = []string{"residential_rotating", "residential_sticky", "isp_static", "datacenter", "mobile"}

// ProductCategory is data.ts productCategoryFor.
var ProductCategory = map[string]string{
	"residential_rotating": "residential",
	"residential_sticky":   "residential",
	"isp_static":           "isp",
	"datacenter":           "datacenter",
	"mobile":               "mobile",
}

// DefaultProduct is the product kind measured for a category when nothing says otherwise.
var DefaultProduct = map[string]string{
	"residential": "residential_rotating",
	"isp":         "isp_static",
	"datacenter":  "datacenter",
	"mobile":      "mobile",
}

// KindLabels is data.ts kindLabels.
var KindLabels = map[string]string{
	"residential": "Residential",
	"datacenter":  "Datacenter",
	"mobile":      "Mobile",
	"isp":         "ISP",
}

// ProductLabels is data.ts productLabels.
var ProductLabels = map[string]string{
	"residential_rotating": "Rotating residential",
	"residential_sticky":   "Sticky residential",
	"isp_static":           "Static ISP",
	"datacenter":           "Dedicated",
	"mobile":               "Mobile",
}

func IsProxyKind(k string) bool {
	for _, v := range ProxyKinds {
		if v == k {
			return true
		}
	}
	return false
}

// Breakdown is ScoreBreakdown, also used for weights.
type Breakdown struct {
	Reliability  Num `json:"reliability"`
	Speed        Num `json:"speed"`
	Uptime       Num `json:"uptime"`
	Coverage     Num `json:"coverage"`
	Diversity    Num `json:"diversity"`
	DcAsns       Num `json:"dcAsns"`
	Authenticity Num `json:"authenticity"`
	Value        Num `json:"value"`
}

// BreakdownKeys in contract order.
var BreakdownKeys = []string{"reliability", "speed", "uptime", "coverage", "diversity", "dcAsns", "authenticity", "value"}

func (b *Breakdown) UnmarshalJSON(d []byte) error {
	type plain Breakdown
	return jsnum.DecodeNA(d, (*plain)(b))
}

// Get returns a component by key.
func (b *Breakdown) Get(k string) Num {
	switch k {
	case "reliability":
		return b.Reliability
	case "speed":
		return b.Speed
	case "uptime":
		return b.Uptime
	case "coverage":
		return b.Coverage
	case "diversity":
		return b.Diversity
	case "dcAsns":
		return b.DcAsns
	case "authenticity":
		return b.Authenticity
	case "value":
		return b.Value
	}
	return jsnum.NaN
}

// Set assigns a component by key; unknown keys are ignored.
func (b *Breakdown) Set(k string, v Num) {
	switch k {
	case "reliability":
		b.Reliability = v
	case "speed":
		b.Speed = v
	case "uptime":
		b.Uptime = v
	case "coverage":
		b.Coverage = v
	case "diversity":
		b.Diversity = v
	case "dcAsns":
		b.DcAsns = v
	case "authenticity":
		b.Authenticity = v
	case "value":
		b.Value = v
	}
}

// NewBreakdown is all-unavailable.
func NewBreakdown() Breakdown {
	var b Breakdown
	jsnum.FillNaN(&b)
	return b
}

type TestScope struct {
	ID             string   `json:"id"`
	Label          Str      `json:"label"`
	Summary        Str      `json:"summary"`
	Cadence        Str      `json:"cadence"`
	Targets        any      `json:"targets"` // []string or "all"
	LiveIPSampling Bool     `json:"liveIpSampling"`
	PoolTracking   Bool     `json:"poolTracking"`
	OverlapAudit   Bool     `json:"overlapAudit"`
	StickinessTest Bool     `json:"stickinessTest"`
	GeoCoverage    Str      `json:"geoCoverage"`
	Rationale      Str      `json:"rationale"`
	NA             []string `json:"na"`
}

type RankingProfile struct {
	ID          string    `json:"id"`
	Label       Str       `json:"label"`
	Description Str       `json:"description"`
	Weights     Breakdown `json:"weights"`
	NA          []string  `json:"na"`
}

type ProviderProduct struct {
	Kind       string `json:"kind"`
	Name       Str    `json:"name"`
	Measured   bool   `json:"measured"`
	URL        Str    `json:"url"`
	PriceLabel Str    `json:"priceLabel"`
	Note       Str    `json:"note"`
}

type PoolOverlap struct {
	Slug              string `json:"slug"`
	Name              Str    `json:"name"`
	SharedIps         Num    `json:"sharedIps"`
	ShareOfOurs       Num    `json:"shareOfOurs"`
	ShareOfTheirs     Num    `json:"shareOfTheirs"`
	RequestsBothSides Num    `json:"requestsBothSides"`
}

func (v *PoolOverlap) UnmarshalJSON(d []byte) error {
	type plain PoolOverlap
	return jsnum.DecodeNA(d, (*plain)(v))
}

type LikelyUpstream struct {
	Slug        string `json:"slug"`
	Name        Str    `json:"name"`
	ShareOfOurs Num    `json:"shareOfOurs"`
}

func (v *LikelyUpstream) UnmarshalJSON(d []byte) error {
	type plain LikelyUpstream
	return jsnum.DecodeNA(d, (*plain)(v))
}

type OverlapSummary struct {
	ExclusiveShare Num             `json:"exclusiveShare"`
	SharedShare    Num             `json:"sharedShare"`
	Overlaps       []PoolOverlap   `json:"overlaps"`
	LikelyUpstream *LikelyUpstream `json:"likelyUpstream"`
}

func (v *OverlapSummary) UnmarshalJSON(d []byte) error {
	type plain OverlapSummary
	if err := jsnum.DecodeNA(d, (*plain)(v)); err != nil {
		return err
	}
	if v.Overlaps == nil {
		v.Overlaps = []PoolOverlap{}
	}
	return nil
}

type ScoreView struct {
	Composite    Num       `json:"composite"`
	Rank         Num       `json:"rank"`
	PreviousRank Num       `json:"previousRank"`
	Breakdown    Breakdown `json:"breakdown"`
	Grade        Str       `json:"grade"`
	// Peers is the ready networks of the kind the score was set against
	// (measured networks; 0 when unknown). Not published as a field: the
	// snapshot's components carry it, and labels are derived from it.
	Peers Num `json:"-"`
}

// KindStanding is every-request.ts kindStanding's label: "#1 of 1 in its kind"
// for a measured network with a ready score alone in its kind (the framework
// leaves it unranked), "#r of N in its kind" with a rank and peers, "#r", or
// "Unranked".
func (s ScoreView) KindStanding() string {
	rank := s.Rank
	if !rank.OK() && s.Composite.OK() && s.Peers == 1 {
		rank = 1
	}
	switch {
	case !rank.OK():
		return "Unranked"
	case s.Peers >= 1:
		return "#" + jsnum.ToFixed(rank.F(), 0) + " of " + jsnum.Locale(s.Peers.F()) + " in its kind"
	}
	return "#" + jsnum.ToFixed(rank.F(), 0)
}

type PoolStats struct {
	UniqueIps24h    Num `json:"uniqueIps24h"`
	UniqueIps30d    Num `json:"uniqueIps30d"`
	ClaimedPoolSize Num `json:"claimedPoolSize"`
	ChurnRate       Num `json:"churnRate"`
	RecurringShare  Num `json:"recurringShare"`
	Countries       Num `json:"countries"`
	Regions         Num `json:"regions"`
	Cities          Num `json:"cities"`
	Asns            Num `json:"asns"`
	RequestsPerIP   Num `json:"requestsPerIp"`
}

func (v *PoolStats) UnmarshalJSON(d []byte) error {
	type plain PoolStats
	return jsnum.DecodeNA(d, (*plain)(v))
}

type PerformanceStats struct {
	SuccessRate         Num `json:"successRate"`
	SuccessRateDelta24h Num `json:"successRateDelta24h"`
	SuccessRateDelta30d Num `json:"successRateDelta30d"`
	LatencyDelta24h     Num `json:"latencyDelta24h"`
	LatencyP50          Num `json:"latencyP50"`
	LatencyP90          Num `json:"latencyP90"`
	LatencyP99          Num `json:"latencyP99"`
	TtfbP50             Num `json:"ttfbP50"`
	CaptchaRate         Num `json:"captchaRate"`
	BlockRate           Num `json:"blockRate"`
	TimeoutRate         Num `json:"timeoutRate"`
	UptimePct           Num `json:"uptimePct"`
	DowntimeMinutes24h  Num `json:"downtimeMinutes24h"`
	Requests24h         Num `json:"requests24h"`
	BandwidthMb24h      Num `json:"bandwidthMb24h"`
}

func (v *PerformanceStats) UnmarshalJSON(d []byte) error {
	type plain PerformanceStats
	return jsnum.DecodeNA(d, (*plain)(v))
}

type PricingPlan struct {
	Catalog     json.RawMessage `json:"catalog,omitempty"`
	Name        Str             `json:"name"`
	Price       Num             `json:"price"`
	Gb          Num             `json:"gb"`
	Ips         Num             `json:"ips"`
	Note        Str             `json:"note"`
	Recommended Bool            `json:"recommended"`
	DiscountPct Num             `json:"discountPct"`
}

type Pricing struct {
	Model             Str           `json:"model"`
	PricePerGb        Num           `json:"pricePerGb"`
	PricePerGbAtScale Num           `json:"pricePerGbAtScale"`
	MinCommit         Num           `json:"minCommit"`
	Trial             Str           `json:"trial"`
	Plans             []PricingPlan `json:"plans"`
	CostPer1kSuccess  Num           `json:"costPer1kSuccess"`
	AvgPayloadKb      Num           `json:"avgPayloadKb"`
	CostBasis         Str           `json:"costBasis"`
	HeadlinePriceRank Num           `json:"headlinePriceRank"`
	ValueRank         Num           `json:"valueRank"`
}

type LeakCheck struct {
	Name   Str  `json:"name"`
	Passed Bool `json:"passed"`
	Detail Str  `json:"detail"`
}

type TrustSignals struct {
	ResidentialAuthenticity Num         `json:"residentialAuthenticity"`
	DatacenterAsnShare      Num         `json:"datacenterAsnShare"`
	ClaimGap                Num         `json:"claimGap"`
	BlacklistHitRate        Num         `json:"blacklistHitRate"`
	StickinessHoldRate      Num         `json:"stickinessHoldRate"`
	StickinessAdvertised    Str         `json:"stickinessAdvertised"`
	LeakChecks              []LeakCheck `json:"leakChecks"`
	KycVerified             Bool        `json:"kycVerified"`
	ConsentDisclosed        Bool        `json:"consentDisclosed"`
}

type SeriesPoint struct {
	T Num `json:"t"`
	V Num `json:"v"`
}

func (v *SeriesPoint) UnmarshalJSON(d []byte) error {
	type plain SeriesPoint
	return jsnum.DecodeNA(d, (*plain)(v))
}

type LatencyPoint struct {
	T   Num `json:"t"`
	P50 Num `json:"p50"`
	P90 Num `json:"p90"`
	P99 Num `json:"p99"`
}

func (v *LatencyPoint) UnmarshalJSON(d []byte) error {
	type plain LatencyPoint
	return jsnum.DecodeNA(d, (*plain)(v))
}

type ProviderSeries struct {
	Latency24h    []LatencyPoint `json:"latency24h"`
	Success24h    []SeriesPoint  `json:"success24h"`
	UniqueIps24h  []SeriesPoint  `json:"uniqueIps24h"`
	UniqueIps30d  []SeriesPoint  `json:"uniqueIps30d"`
	Latency30d    []SeriesPoint  `json:"latency30d"`
	Success30d    []SeriesPoint  `json:"success30d"`
	PricePerGb90d []SeriesPoint  `json:"pricePerGb90d"`
}

// Normalize replaces nil slices with empty ones.
func (s *ProviderSeries) Normalize() {
	if s.Latency24h == nil {
		s.Latency24h = []LatencyPoint{}
	}
	for _, p := range []*[]SeriesPoint{&s.Success24h, &s.UniqueIps24h, &s.UniqueIps30d, &s.Latency30d, &s.Success30d, &s.PricePerGb90d} {
		if *p == nil {
			*p = []SeriesPoint{}
		}
	}
}

type IPSample struct {
	IP                  Str  `json:"ip"`
	CountryCode         Str  `json:"countryCode"`
	Country             Str  `json:"country"`
	City                Str  `json:"city"`
	Region              Str  `json:"region"`
	Asn                 Num  `json:"asn"`
	AsnName             Str  `json:"asnName"`
	Kind                Str  `json:"kind"`
	Misclassified       Bool `json:"misclassified"`
	Blacklisted         Bool `json:"blacklisted"`
	LatencyMs           Num  `json:"latencyMs"`
	FirstSeenAgoSeconds Num  `json:"firstSeenAgoSeconds"`
	LastSeenAgoSeconds  Num  `json:"lastSeenAgoSeconds"`
	RequestsServed      Num  `json:"requestsServed"`
}

func (v *IPSample) UnmarshalJSON(d []byte) error {
	type plain IPSample
	return jsnum.DecodeNA(d, (*plain)(v))
}

type Incident struct {
	ID                string `json:"id"`
	StartedAgoSeconds Num    `json:"startedAgoSeconds"`
	DurationMinutes   Num    `json:"durationMinutes"`
	Severity          Str    `json:"severity"`
	Scope             Str    `json:"scope"`
	Summary           Str    `json:"summary"`
	TroughSuccessRate Num    `json:"troughSuccessRate"`
	Impact            Num    `json:"impact"`
}

func (v *Incident) UnmarshalJSON(d []byte) error {
	type plain Incident
	return jsnum.DecodeNA(d, (*plain)(v))
}

type GeoBucket struct {
	CountryCode string `json:"countryCode"`
	Country     Str    `json:"country"`
	Ips         Num    `json:"ips"`
	Share       Num    `json:"share"`
	LatencyP50  Num    `json:"latencyP50"`
	SuccessRate Num    `json:"successRate"`
}

func (v *GeoBucket) UnmarshalJSON(d []byte) error {
	type plain GeoBucket
	return jsnum.DecodeNA(d, (*plain)(v))
}

type AsnBucket struct {
	Asn   Num    `json:"asn"`
	Name  Str    `json:"name"`
	Kind  string `json:"kind"`
	Ips   Num    `json:"ips"`
	Share Num    `json:"share"`
}

func (v *AsnBucket) UnmarshalJSON(d []byte) error {
	type plain AsnBucket
	return jsnum.DecodeNA(d, (*plain)(v))
}

// Measured is the Performance-through-Pricing block shared by Provider (the
// headline product) and ProductMeasurement.
type Measured struct {
	Pool        PoolStats         `json:"pool"`
	Performance PerformanceStats  `json:"performance"`
	Pricing     Pricing           `json:"pricing"`
	Trust       TrustSignals      `json:"trust"`
	Targets     []TargetBenchmark `json:"targets"`
	Series      ProviderSeries    `json:"series"`
	IPSamples   []IPSample        `json:"ipSamples"`
	Incidents   []Incident        `json:"incidents"`
	Geo         []GeoBucket       `json:"geo"`
	Asns        []AsnBucket       `json:"asns"`
	Overlap     OverlapSummary    `json:"overlap"`
}

// Normalize replaces nil slices with empty ones.
func (m *Measured) Normalize() {
	if m.Pricing.Plans == nil {
		m.Pricing.Plans = []PricingPlan{}
	}
	if m.Trust.LeakChecks == nil {
		m.Trust.LeakChecks = []LeakCheck{}
	}
	if m.Targets == nil {
		m.Targets = []TargetBenchmark{}
	}
	m.Series.Normalize()
	if m.IPSamples == nil {
		m.IPSamples = []IPSample{}
	}
	if m.Incidents == nil {
		m.Incidents = []Incident{}
	}
	if m.Geo == nil {
		m.Geo = []GeoBucket{}
	}
	if m.Asns == nil {
		m.Asns = []AsnBucket{}
	}
	if m.Overlap.Overlaps == nil {
		m.Overlap.Overlaps = []PoolOverlap{}
	}
}

// EmptyMeasured is every field unavailable.
func EmptyMeasured() Measured {
	var m Measured
	jsnum.FillNaN(&m)
	m.Normalize()
	return m
}

type ProductMeasurement struct {
	Kind    string `json:"kind"`
	Product string `json:"product"`
	Measured
}

// CountryLatencyRank compares observed-country p50 within one measured product
// category, cohort and reporting period. Ties share a competition rank.
type CountryLatencyRank struct {
	P50   Num `json:"p50"`
	Rank  Num `json:"rank"`
	Peers int `json:"peers"`
}

// Rank is a competition rank among measured peers (ties share a rank). Rank
// is NaN when the product has no value; Peers counts the products with one.
type Rank struct {
	Rank  Num `json:"rank"`
	Peers int `json:"peers"`
}

// TileRanks ranks one measured product's coverage counts (higher is better)
// and headline latency p50 (lower is better) within its product category,
// cohort and reporting period.
type TileRanks struct {
	UniqueIps Rank `json:"uniqueIps"`
	Countries Rank `json:"countries"`
	Regions   Rank `json:"regions"`
	Cities    Rank `json:"cities"`
	Asns      Rank `json:"asns"`
	Latency   Rank `json:"latency"`
}

// CoverageRow is one observed city x ASN of a network's headline group in one
// range (the network page's coverage evidence table): written as
// [countryCode, country, region, city, asn, asnName, usageType, ips,
// latencyP50] to keep the long lists small. Text is null when unknown, as are
// the ASN and the numbers.
type CoverageRow struct {
	CountryCode, Country, Region, City Str
	Asn                                Num
	AsnName, UsageType                 Str
	Ips, LatencyP50                    Num
}

func (r CoverageRow) MarshalJSON() ([]byte, error) {
	return json.Marshal([...]any{r.CountryCode, r.Country, r.Region, r.City, r.Asn, r.AsnName, r.UsageType, r.Ips, r.LatencyP50})
}

type ReviewSummary struct {
	Count         Num    `json:"count"`
	VerifiedCount Num    `json:"verifiedCount"`
	Mean          Num    `json:"mean"`
	Distribution  [5]Num `json:"distribution"`
	VerifiedMean  Num    `json:"verifiedMean"`
}

type Provider struct {
	Source              string            `json:"source"`
	Slug                string            `json:"slug"`
	Name                string            `json:"name"`
	Tagline             Str               `json:"tagline"`
	Kinds               []string          `json:"kinds"`
	PrimaryKind         string            `json:"primaryKind"`
	HQ                  Str               `json:"hq"`
	Founded             Num               `json:"founded"`
	Claim               Str               `json:"claim"`
	Verdict             Str               `json:"verdict"`
	Strengths           []string          `json:"strengths"`
	Weaknesses          []string          `json:"weaknesses"`
	BestFor             []string          `json:"bestFor"`
	Website             Str               `json:"website"`
	Products            []ProviderProduct `json:"products"`
	MeasuredProduct     string            `json:"measuredProduct"`
	ProductMeasurements *MeasurementMap   `json:"productMeasurements"`
	Score               ScoreView         `json:"score"`
	Measured
	Reviews             ReviewSummary                                       `json:"reviews"`
	CountryLatencyRanks map[string]map[string]map[string]CountryLatencyRank `json:"countryLatencyRanks,omitempty"`
	// Precomputed measured ranks by proxy type and time range.
	TileRanks map[string]map[string]TileRanks `json:"tileRanks,omitempty"`
	// CoverageEvidence is, by proxy type and time range, every observed city x
	// ASN row of the type's headline group, most IPs first. Builds before
	// payload v4 only: a v4 build's rows are the dataset's lists.*.city_asn.
	CoverageEvidence map[string]map[string][]CoverageRow `json:"coverageEvidence,omitempty"`
	// Framework identity, null for unlinked catalog rows.
	Framework *FrameworkLink `json:"framework"`
	NA        []string       `json:"na"`
}

type FrameworkLink struct {
	Provider string   `json:"provider"`
	Products []string `json:"products"`
}

type TapeEvent struct {
	ID           string   `json:"id"`
	Kind         string   `json:"kind"`
	ProviderSlug string   `json:"providerSlug"`
	ProviderName string   `json:"providerName"`
	Product      string   `json:"product"`
	Title        string   `json:"title"`
	Detail       string   `json:"detail"`
	Signal       Num      `json:"signal"`
	Value        string   `json:"value"`
	AgoSeconds   Num      `json:"agoSeconds"`
	At           Str      `json:"at"`
	Source       string   `json:"source"`
	NA           []string `json:"na"`
}

type ChangeEvent struct {
	ID           string   `json:"id"`
	Kind         string   `json:"kind"`
	ProviderSlug Str      `json:"providerSlug"`
	ProviderName Str      `json:"providerName"`
	Headline     string   `json:"headline"`
	Detail       string   `json:"detail"`
	Delta        Num      `json:"delta"`
	Unit         string   `json:"unit"`
	AgoSeconds   Num      `json:"agoSeconds"`
	Severity     string   `json:"severity"`
	Source       string   `json:"source"`
	NA           []string `json:"na"`
}

type UserReview struct {
	ID            string   `json:"id"`
	ProviderSlug  string   `json:"providerSlug"`
	Author        string   `json:"author"`
	Role          string   `json:"role"`
	Rating        Num      `json:"rating"`
	Title         string   `json:"title"`
	Body          string   `json:"body"`
	Product       string   `json:"product"`
	UseCase       string   `json:"useCase"`
	MonthlySpend  string   `json:"monthlySpend"`
	MonthsUsed    Num      `json:"monthsUsed"`
	Verification  string   `json:"verification"`
	InvoiceMonth  Str      `json:"invoiceMonth"`
	LinkedRunID   Str      `json:"linkedRunId"`
	HelpfulCount  Num      `json:"helpfulCount"`
	ProviderReply Str      `json:"providerReply"`
	AgoSeconds    Num      `json:"agoSeconds"`
	Source        string   `json:"source"`
	NA            []string `json:"na"`
}

type CommunityRun struct {
	ID               string            `json:"id"`
	ProviderSlug     string            `json:"providerSlug"`
	ProviderName     string            `json:"providerName"`
	SubmittedBy      string            `json:"submittedBy"`
	Region           string            `json:"region"`
	Product          string            `json:"product"`
	PlanTier         string            `json:"planTier"`
	CollectorVersion string            `json:"collectorVersion"`
	Status           string            `json:"status"`
	RejectionReason  Str               `json:"rejectionReason"`
	Requests         Num               `json:"requests"`
	SuccessRate      Num               `json:"successRate"`
	LatencyP50       Num               `json:"latencyP50"`
	UniqueIps        Num               `json:"uniqueIps"`
	OverlapShare     Num               `json:"overlapShare"`
	Targets          []TargetBenchmark `json:"targets"`
	SuccessSeries    []SeriesPoint     `json:"successSeries"`
	LatencySeries    []LatencyPoint    `json:"latencySeries"`
	DeltaSuccessPts  Num               `json:"deltaSuccessPts"`
	DeltaLatencyPct  Num               `json:"deltaLatencyPct"`
	AgoSeconds       Num               `json:"agoSeconds"`
	Source           string            `json:"source"`
	NA               []string          `json:"na"`
}

// MeasurementMap is productMeasurements with insertion-ordered keys.
type MeasurementMap struct {
	Keys []string
	M    map[string]*ProductMeasurement
}

func NewMeasurementMap() *MeasurementMap {
	return &MeasurementMap{Keys: []string{}, M: map[string]*ProductMeasurement{}}
}

func (m *MeasurementMap) Set(k string, v *ProductMeasurement) {
	if _, ok := m.M[k]; !ok {
		m.Keys = append(m.Keys, k)
	}
	m.M[k] = v
}

func (m *MeasurementMap) Get(k string) *ProductMeasurement {
	if m == nil {
		return nil
	}
	return m.M[k]
}

func (m *MeasurementMap) MarshalJSON() ([]byte, error) {
	var b bytes.Buffer
	b.WriteByte('{')
	for i, k := range m.Keys {
		if i > 0 {
			b.WriteByte(',')
		}
		key, _ := json.Marshal(k)
		b.Write(key)
		b.WriteByte(':')
		v, err := json.Marshal(m.M[k])
		if err != nil {
			return nil, err
		}
		b.Write(v)
	}
	b.WriteByte('}')
	return b.Bytes(), nil
}
