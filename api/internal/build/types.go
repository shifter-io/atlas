// Package build turns one consistent read of the database (store.Inputs) into
// every response the API serves. It is pure: no I/O, so a build can be tested
// against fixtures and compared with the TypeScript it ports.
package build

import (
	"github.com/ice-zeus/milf-website/api/internal/jsnum"
	"github.com/ice-zeus/milf-website/api/internal/model"
)

type Num = jsnum.Num

var nan = jsnum.NaN

// RangeMeta is src/lib/ranges.ts RangeMeta.
type RangeMeta struct {
	ID         string   `json:"id"`
	Label      string   `json:"label"`
	Short      string   `json:"short"`
	Days       int      `json:"days"`
	Bucket     string   `json:"bucket"`
	Resolution string   `json:"resolution"`
	Axis       []string `json:"axis"`
	Phrase     string   `json:"phrase"`
}

// Ranges is ranges.ts RANGES.
var Ranges = []RangeMeta{
	{ID: "24h", Label: "Last 24 hours", Short: "24h", Days: 1, Bucket: "5-minute buckets", Resolution: "5 min", Axis: []string{"24h ago", "18h", "12h", "6h", "now"}, Phrase: "the last 24 hours"},
	{ID: "7d", Label: "Last 7 days", Short: "7d", Days: 7, Bucket: "Daily buckets", Resolution: "1 day", Axis: []string{"7d ago", "5d", "3d", "now"}, Phrase: "the last 7 days"},
	{ID: "30d", Label: "Last 30 days", Short: "30d", Days: 30, Bucket: "Daily buckets", Resolution: "1 day", Axis: []string{"30d ago", "20d", "10d", "now"}, Phrase: "the last 30 days"},
}

func rangeMeta(id string) RangeMeta {
	for _, r := range Ranges {
		if r.ID == id {
			return r
		}
	}
	return Ranges[len(Ranges)-1]
}

// IncidentBand is window.ts IncidentBand.
type IncidentBand struct {
	From  Num    `json:"from"`
	To    Num    `json:"to"`
	Label string `json:"label"`
}

// WindowStats is window.ts WindowStats.
type WindowStats struct {
	Range            RangeMeta            `json:"range"`
	SuccessRate      Num                  `json:"successRate"`
	SuccessDeltaPts  Num                  `json:"successDeltaPts"`
	LatencyP50       Num                  `json:"latencyP50"`
	LatencyP90       Num                  `json:"latencyP90"`
	LatencyP99       Num                  `json:"latencyP99"`
	LatencyDeltaPct  Num                  `json:"latencyDeltaPct"`
	UniqueIps        Num                  `json:"uniqueIps"`
	ChurnRate        Num                  `json:"churnRate"`
	RecurringShare   Num                  `json:"recurringShare"`
	Requests         Num                  `json:"requests"`
	BandwidthMb      Num                  `json:"bandwidthMb"`
	CostPer1kSuccess Num                  `json:"costPer1kSuccess"`
	UptimePct        Num                  `json:"uptimePct"`
	DowntimeMinutes  Num                  `json:"downtimeMinutes"`
	IncidentCount    Num                  `json:"incidentCount"`
	Bands            []IncidentBand       `json:"bands"`
	Success          []model.SeriesPoint  `json:"success"`
	Latency          []model.LatencyPoint `json:"latency"`
	Pool             []model.SeriesPoint  `json:"pool"`
}

// FleetWindow is window.ts FleetWindow.
type FleetWindow struct {
	Range             RangeMeta           `json:"range"`
	Requests          Num                 `json:"requests"`
	UniqueIps         Num                 `json:"uniqueIps"`
	MedianLatency     Num                 `json:"medianLatency"`
	MedianSuccessRate Num                 `json:"medianSuccessRate"`
	BandwidthGb       Num                 `json:"bandwidthGb"`
	Success           []model.SeriesPoint `json:"success"`
	Latency           []model.SeriesPoint `json:"latency"`
	// Basis names the providers the fleet figures were computed over.
	Basis string `json:"basis"`
}

// CostBoard is window.ts CostBoard, with ranks as a slug map.
type CostBoard struct {
	Range   string         `json:"range"`
	Ranks   map[string]int `json:"ranks"`
	Best    Num            `json:"best"`
	Dearest Num            `json:"dearest"`
}

// SparkSeries is overview.ts SparkSeries.
type SparkSeries struct {
	Values []Num    `json:"values"`
	Labels []string `json:"labels"`
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

type Bill struct {
	Provider ProviderRef `json:"provider"`
	Bill     Num         `json:"bill"`
}

type Bills struct {
	All     []Bill `json:"all"`
	Best    *Bill  `json:"best"`
	Worst   *Bill  `json:"worst"`
	SpreadX Num    `json:"spreadX"`
}

// FleetView is overview.ts FleetView.
type FleetView struct {
	Range         RangeMeta   `json:"range"`
	Fleet         FleetWindow `json:"fleet"`
	LatencySpark  SparkSeries `json:"latencySpark"`
	SuccessSpark  SparkSeries `json:"successSpark"`
	HeadlineSpark SparkSeries `json:"headlineSpark"`
	Fastest       StatPick    `json:"fastest"`
	MostReliable  StatPick    `json:"mostReliable"`
	Cheapest      CostPick    `json:"cheapest"`
	Dearest       CostPick    `json:"dearest"`
	Bills         Bills       `json:"bills"`
}

// ProviderSummary is one leaderboard row.
type ProviderSummary struct {
	Source          string                  `json:"source"`
	Slug            string                  `json:"slug"`
	Name            string                  `json:"name"`
	Tagline         model.Str               `json:"tagline"`
	Kinds           []string                `json:"kinds"`
	PrimaryKind     string                  `json:"primaryKind"`
	MeasuredProduct string                  `json:"measuredProduct"`
	HQ              model.Str               `json:"hq"`
	Score           model.ScoreView         `json:"score"`
	PricePerGb      Num                     `json:"pricePerGb"`
	Windows         map[string]*WindowStats `json:"windows"`
	ProductKinds    []string                `json:"productKinds"`
	NACount         int                     `json:"naCount"`
}

// Overview is everything the Overview page reads.
type Overview struct {
	GeneratedAt string                 `json:"generatedAt"`
	Simulated   bool                   `json:"simulated"`
	Snapshot    model.SnapshotMeta     `json:"snapshot"`
	Totals      model.FleetTotals      `json:"totals"`
	Weights     model.Breakdown        `json:"weights"`
	Profiles    []model.RankingProfile `json:"profiles"`
	Ranges      []RangeMeta            `json:"ranges"`
	FleetViews  []FleetView            `json:"fleetViews"`
	CostBoards  []CostBoard            `json:"costBoards"`
	Leaderboard []ProviderSummary      `json:"leaderboard"`
	Tape        []model.TapeEvent      `json:"tape"`
	NA          []string               `json:"na"`
}

// ProviderDetail is /v1/providers/{slug}: the Provider plus everything a
// network page lists about it.
type ProviderDetail struct {
	Provider      *model.Provider                    `json:"provider"`
	Windows       map[string]map[string]*WindowStats `json:"windows"`
	Reviews       []model.UserReview                 `json:"reviews"`
	CommunityRuns []model.CommunityRun               `json:"communityRuns"`
	Changelog     []model.ChangeEvent                `json:"changelog"`
	Tape          []model.TapeEvent                  `json:"tape"`
	GeneratedAt   string                             `json:"generatedAt"`
}
