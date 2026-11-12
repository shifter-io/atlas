package model

// The blocks of Measured: pool, performance, pricing and trust stats, the
// time series, IP samples, incidents and geo and ASN buckets.

import (
	"encoding/json"

	"github.com/Zeus-Development/proxy-index/api/internal/jsnum"
)

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
	// SuccessScoreDelta24h and 30d are the 24h and 30d windows' overall
	// success score minus the previous published build's (SUCCESS-SCORE-DELTA-1).
	SuccessScoreDelta24h Num `json:"successScoreDelta24h"`
	SuccessScoreDelta30d Num `json:"successScoreDelta30d"`
	LatencyDelta24h      Num `json:"latencyDelta24h"`
	LatencyP50           Num `json:"latencyP50"`
	LatencyMean          Num `json:"latencyMean"`
	LatencyP90           Num `json:"latencyP90"`
	LatencyP99           Num `json:"latencyP99"`
	TtfbP50              Num `json:"ttfbP50"`
	CaptchaRate          Num `json:"captchaRate"`
	BlockRate            Num `json:"blockRate"`
	TimeoutRate          Num `json:"timeoutRate"`
	UptimePct            Num `json:"uptimePct"`
	DowntimeMinutes24h   Num `json:"downtimeMinutes24h"`
	Requests24h          Num `json:"requests24h"`
	BandwidthMb24h       Num `json:"bandwidthMb24h"`
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
