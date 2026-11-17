// Package build turns one consistent read of the database (store.Inputs) into
// every response the API serves. It is pure: no I/O, so a build can be tested
// against fixtures and compared with the TypeScript it ports.
package build

import (
	"github.com/Zeus-Development/atlas/api/internal/jsnum"
	"github.com/Zeus-Development/atlas/api/internal/model"
)

type Num = jsnum.Num

var nan = jsnum.NaN

// RangeMeta is src/lib/ranges.ts RangeMeta: its id and days. Its English
// label, bucket, resolution and phrase (API-PROSE-REMOVAL-1, #962), its short
// form and its axis ticks (TIME-LABELS-REMOVE-1, #988) are not served: the
// website words and ticks a range from its id and days.
type RangeMeta struct {
	ID   string `json:"id"`
	Days int    `json:"days"`
}

// Ranges is ranges.ts RANGES.
var Ranges = []RangeMeta{{ID: "24h", Days: 1}, {ID: "7d", Days: 7}, {ID: "30d", Days: 30}}

func rangeMeta(id string) RangeMeta {
	for _, r := range Ranges {
		if r.ID == id {
			return r
		}
	}
	return Ranges[len(Ranges)-1]
}

// IncidentBand is window.ts IncidentBand: an outage's span on the chart, its
// severity (incident_severity code) and the country it took (ISO code, null
// when unknown).
type IncidentBand struct {
	From     Num       `json:"from"`
	To       Num       `json:"to"`
	Severity model.Str `json:"severity"`
	Country  model.Str `json:"country"`
}

// WindowStats is window.ts WindowStats.
type WindowStats struct {
	Range           RangeMeta `json:"range"`
	SuccessRate     Num       `json:"-"` // internal (SUCCESS-RATE-REMOVE-1, #900): successScore is served
	SuccessDeltaPts Num       `json:"-"` // internal (#900): successScoreDelta is served
	LatencyP50      Num       `json:"latencyP50"`
	LatencyP90      Num       `json:"latencyP90"`
	LatencyP99      Num       `json:"latencyP99"`
	// Light-load latency (the pulses): the headline latency since
	// HEADLINE-PULSE-1, kept so that no client breaks.
	LightLatencyP50 Num `json:"lightLatencyP50"`
	LightLatencyP90 Num `json:"lightLatencyP90"`
	LightLatencyP99 Num `json:"lightLatencyP99"`
	// LatencyTarget names the target the latency fields are for (measured
	// networks: generic_http, the only target with a latency, see
	// latencyMetrics); null without a snapshot product.
	LatencyTarget   *string `json:"latencyTarget"`
	LatencyDeltaPct Num     `json:"latencyDeltaPct"`
	// LatencyDeltaBasis is the periods LatencyDeltaPct compares: day, week or
	// month (LATENCY-DELTA-FALLBACK-1); null when it is null or not the day
	// rule's.
	LatencyDeltaBasis model.Str `json:"latencyDeltaBasis"`
	// LatencyDeltaAsOf is the last UTC day (YYYY-MM-DD) the periods of
	// LatencyDeltaPct cover: the day before the calculation's day, earlier
	// for a value carried on (LATENCY-ALWAYS-1); null with LatencyDeltaPct.
	LatencyDeltaAsOf model.Str `json:"latencyDeltaAsOf"`
	UniqueIps        Num       `json:"uniqueIps"`
	ChurnRate        Num       `json:"churnRate"`
	RecurringShare   Num       `json:"recurringShare"`
	Requests         Num       `json:"requests"`
	BandwidthMb      Num       `json:"bandwidthMb"`
	// RequestSuccessRate (internal) is the range's successes ÷ requests, what
	// attemptsPerSuccess divides by, as every cost per success does
	// (requestRate); SuccessRate is the mean of the time-slot rates.
	RequestSuccessRate Num `json:"-"`
	// CostPer1kSuccess is the plain-HTTP cost: internal only, not served
	// (#950) and no longer the value rank's input (#951).
	CostPer1kSuccess Num `json:"-"`
	UptimePct        Num `json:"uptimePct"`
	DowntimeMinutes  Num `json:"downtimeMinutes"`
	IncidentCount    Num `json:"incidentCount"`
	WindowFigures
	Bands        []IncidentBand       `json:"bands"`
	Success      []model.SeriesPoint  `json:"-"` // internal (#900): the chart answer serves its points' scores
	Availability []model.SeriesPoint  `json:"availability"`
	Latency      []model.LatencyPoint `json:"latency"`
	Pool         []model.SeriesPoint  `json:"pool"`
	// UnderLoad is the latency under load per series bucket, served by the
	// chart answer (latencyUnderLoad) only.
	UnderLoad []model.LatencyPoint `json:"-"`
	// successEdges and latencyEdges are the means SuccessDeltaPts and
	// LatencyDeltaPct compare, for the changelog's values only.
	successEdges, latencyEdges trendEdges
	// latencyWhy is the reason code of a null LatencyDeltaPct where its rule
	// gives one (dayLatencyDelta), "" otherwise.
	latencyWhy string
}

// trendEdges is the opening and the closing fifth's mean of a trend.
type trendEdges [2]Num

// FleetWindow is window.ts FleetWindow.
type FleetWindow struct {
	Range             RangeMeta           `json:"range"`
	Requests          Num                 `json:"requests"`
	UniqueIps         Num                 `json:"uniqueIps"`
	MedianLatency     Num                 `json:"medianLatency"`
	MedianSuccessRate Num                 `json:"-"` // internal (SUCCESS-RATE-REMOVE-1, #900)
	BandwidthGb       Num                 `json:"bandwidthGb"`
	Success           []model.SeriesPoint `json:"-"` // internal (#900)
	Latency           []model.SeriesPoint `json:"latency"`
	// Basis names the providers the fleet figures were computed over.
	Basis string `json:"basis"`
}

// SparkSeries is overview.ts SparkSeries: its values and each point's time in
// minutes before the build's as-of time (SPARK-TIMES-1); the English point
// labels went in TIME-LABELS-REMOVE-1 (#988).
type SparkSeries struct {
	Values []Num `json:"values"`
	Times  []Num `json:"times"`
}

// ProviderRef names a provider inside derived views.
type ProviderRef struct {
	Slug   string `json:"slug"`
	Name   string `json:"name"`
	Source string `json:"source"`
}

type StatPick struct {
	Provider *ProviderRef `json:"provider"`
	Stats    *WindowStats `json:"stats"`
}

type CostPick struct {
	Provider *ProviderRef `json:"provider"`
	Cost     Num          `json:"cost"`
}

// FleetView is overview.ts FleetView.
type FleetView struct {
	Range         RangeMeta   `json:"range"`
	Fleet         FleetWindow `json:"fleet"`
	LatencySpark  SparkSeries `json:"latencySpark"`
	SuccessSpark  SparkSeries `json:"-"` // internal (#900): /v1/home serves its score spark
	HeadlineSpark SparkSeries `json:"headlineSpark"`
	Fastest       StatPick    `json:"fastest"`
	MostReliable  StatPick    `json:"mostReliable"`
	Cheapest      CostPick    `json:"cheapest"`
	Dearest       CostPick    `json:"dearest"`
}

// ProviderDetail is a Provider plus everything its network page lists about
// it: the source of /v1/networks/{slug} (profile, stats, records) and of its
// chart answers.
type ProviderDetail struct {
	Provider      *model.Provider                    `json:"provider"`
	Windows       map[string]map[string]*WindowStats `json:"windows"`
	Reviews       []model.UserReview                 `json:"reviews"`
	CommunityRuns []model.CommunityRun               `json:"communityRuns"`
	Changelog     []model.ChangeEvent                `json:"changelog"`
	Tape          []model.TapeEvent                  `json:"tape"`
	GeneratedAt   string                             `json:"generatedAt"`
}
