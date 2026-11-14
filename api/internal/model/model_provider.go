package model

import (
	"bytes"
	"encoding/json"

	"github.com/Zeus-Development/atlas/api/internal/jsnum"
)

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
	// BrowserOverall is each engine's overall browser score; not written when
	// the snapshot has none, so an older snapshot's answer is unchanged.
	BrowserOverall map[string]BrowserOverall `json:"browserOverall,omitempty"`
	// SuccessScore is the overall success score; not written when the
	// snapshot has none.
	SuccessScore *SuccessScore `json:"successScore,omitempty"`
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
// [countryCode, region, city, asn, asnName, usageType, ips, latencyP50] to
// keep the long lists small. Text is null when unknown, as are
// the ASN and the numbers.
type CoverageRow struct {
	CountryCode, Region, City Str
	Asn                       Num
	AsnName, UsageType        Str
	Ips, LatencyP50           Num
}

func (r CoverageRow) MarshalJSON() ([]byte, error) {
	return json.Marshal([...]any{r.CountryCode, r.Region, r.City, r.Asn, r.AsnName, r.UsageType, r.Ips, r.LatencyP50})
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
	Kinds               []string          `json:"kinds"`
	PrimaryKind         string            `json:"primaryKind"`
	HQCountry           Str               `json:"hqCountry"` // ISO code
	HQCity              Str               `json:"hqCity"`    // a name; null where the catalog names no city
	Founded             Num               `json:"founded"`
	UseCases            []string          `json:"useCases"` // use_case codes, the network's own order
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
	// Framework identity, null for unlinked catalog rows.
	Framework *FrameworkLink `json:"framework"`
	NA        []string       `json:"na"`
}

type FrameworkLink struct {
	Provider string   `json:"provider"`
	Products []string `json:"products"`
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
