package model

import "encoding/json"

// The dataset's top-level types: /v1/dataset and its file (contract K4).

type FleetTotals struct {
	Providers              Num `json:"providers"`
	Requests24h            Num `json:"requests24h"`
	UniqueIps24h           Num `json:"uniqueIps24h"`
	Countries              Num `json:"countries"`
	MedianLatency          Num `json:"medianLatency"`
	MedianSuccessRate      Num `json:"-"` // internal (SUCCESS-RATE-REMOVE-1, #900)
	Incidents24h           Num `json:"incidents24h"`
	BandwidthGb24h         Num `json:"bandwidthGb24h"`
	MisclassifiedShare     Num `json:"misclassifiedShare"`
	DistinctIpsAcrossFleet Num `json:"distinctIpsAcrossFleet"`
	FleetDuplicationShare  Num `json:"fleetDuplicationShare"`
	LikelyResellers        Num `json:"likelyResellers"`
	Reviews                Num `json:"reviews"`
	VerifiedReviews        Num `json:"verifiedReviews"`
	CommunityRuns          Num `json:"communityRuns"`
}

// SnapshotMeta describes the published framework calculation the measured
// numbers come from (all null when nothing is published).
type SnapshotMeta struct {
	Available      bool `json:"available"`
	SnapshotID     Str  `json:"snapshotId"`
	SchemaVersion  Num  `json:"schemaVersion"`
	AsOf           Str  `json:"asOf"`
	GeneratedAt    Str  `json:"generatedAt"`
	RefreshSeconds Num  `json:"refreshSeconds"`
	MethodologyID  Str  `json:"methodologyId"`
	Readiness      Str  `json:"readiness"`
	ContentHash    Str  `json:"contentHash"`
}

type MeasuredMarket struct {
	Authenticity Num             `json:"authenticity"`
	TargetCount  Num             `json:"targetCount"`
	Views        json.RawMessage `json:"views"`
}

type Dataset struct {
	SchemaVersion    string           `json:"schemaVersion"`
	GeneratedAt      string           `json:"generatedAt"`
	Seed             Num              `json:"seed"`
	Simulated        bool             `json:"simulated"` // always false (the demo fixtures are gone)
	RunID            Str              `json:"runId"`
	CollectorVersion Str              `json:"collectorVersion"`
	WindowHours      Num              `json:"windowHours"`
	Targets          []TargetMeta     `json:"targets"`
	Scopes           []TestScope      `json:"scopes"`
	Weights          Breakdown        `json:"weights"`
	Profiles         []RankingProfile `json:"profiles"`
	Totals           FleetTotals      `json:"totals"`
	MeasuredTotals   FleetTotals      `json:"measuredTotals"`
	MeasuredMarket   MeasuredMarket   `json:"measuredMarket"`
	Providers        []*Provider      `json:"providers"`
	Tape             []TapeEvent      `json:"tape"`
	Changelog        []ChangeEvent    `json:"changelog"`
	Reviews          []UserReview     `json:"reviews"`
	CommunityRuns    []CommunityRun   `json:"communityRuns"`
	// Pages is every page answer the site draws, by the path that serves it
	// (build.DatasetPages), so the file alone regenerates each one.
	Pages any `json:"pages"`
	// Lists is every network list's total, page count and first page as
	// compact arrays (build.DatasetLists, streamed by build.WriteDataset); {}
	// without a snapshot.
	Lists    any          `json:"lists"`
	Snapshot SnapshotMeta `json:"snapshot"`
	NA       []string     `json:"na"`
}
