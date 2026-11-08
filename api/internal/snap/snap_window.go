package snap

import (
	"encoding/json"

	"github.com/Zeus-Development/proxy-index/api/internal/jsnum"
)

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
// The shares and page loads are those of one engine, Engine (the one with the
// most completed tries); Engines holds each engine's own, never added
// together. Both are empty in a snapshot from before the engine split.
// Scores are Engine's rank-based scores (TARGET-SCORES-1), nil in a snapshot
// from before them.
type BrowserResult struct {
	OutcomeRates OutcomeRates            `json:"outcome_rates"`
	PageLoadMs   Latency                 `json:"page_load_ms"`
	Group        *string                 `json:"group"`
	Sources      []string                `json:"sources"`
	Engine       string                  `json:"engine"`
	Engines      map[string]EngineResult `json:"engines"`
	Scores       *TargetScores           `json:"scores"`
}

func (v *BrowserResult) UnmarshalJSON(d []byte) error {
	type plain BrowserResult
	return jsnum.DecodeNA(d, (*plain)(v))
}

// EngineResult is one engine's figures of a browser target (browser.engines.<name>).
type EngineResult struct {
	Completed    Num           `json:"completed"`
	Successes    Num           `json:"successes"`
	OutcomeRates OutcomeRates  `json:"outcome_rates"`
	PageLoadMs   Latency       `json:"page_load_ms"`
	Scores       *TargetScores `json:"scores"`
}

func (v *EngineResult) UnmarshalJSON(d []byte) error {
	type plain EngineResult
	return jsnum.DecodeNA(d, (*plain)(v))
}

// TargetScores are one engine's rank-based 1-10 scores of a browser target
// in one window, among the networks ranked for it (TARGET-SCORES-1): success
// (ok, higher is better), challenge and block (hard block, lower is better).
type TargetScores struct {
	Success   MetricScore `json:"success"`
	Challenge MetricScore `json:"challenge"`
	Block     MetricScore `json:"block"`
}

// MetricScore is one metric's score. Score, Rank (fractional on a tie) and
// RankedOf (M, the networks ranked) are null unless Status is "ranked"; the
// other statuses are "not_enough_data", "too_few_networks" and "all_similar".
type MetricScore struct {
	Rate     Num    `json:"rate"`
	Score    Num    `json:"score"`
	Rank     Num    `json:"rank"`
	RankedOf Num    `json:"ranked_of"`
	Status   string `json:"status"`
}

func (v *MetricScore) UnmarshalJSON(d []byte) error {
	type plain MetricScore
	return jsnum.DecodeNA(d, (*plain)(v))
}

// BrowserOverall is a network's overall browser score for one engine: the
// difficulty-weighted mean of its ranked targets' success scores, over
// Targets with Weights (each target's difficulty).
type BrowserOverall struct {
	Success OverallScore   `json:"success"`
	Targets []string       `json:"targets"`
	Weights map[string]Num `json:"weights"`
}

// OverallScore is the overall success score, its rank among the networks
// with one and how many those are.
type OverallScore struct {
	Score    Num    `json:"score"`
	Rank     Num    `json:"rank"`
	RankedOf Num    `json:"ranked_of"`
	Status   string `json:"status"`
}

func (v *OverallScore) UnmarshalJSON(d []byte) error {
	type plain OverallScore
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
	UsageType   *string `json:"usage_type"`
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
	// Peers is a v4 network window's overlap with each network of its kind
	// it shares IPs with (nil in a group window: its pairs are the
	// snapshot's overlap rows).
	Peers []OverlapPeer `json:"peers"`
}

// OverlapPeer is one row of a network window's overlap.peers (payload v4).
type OverlapPeer struct {
	Provider  string `json:"provider"`
	ProductID Num    `json:"product_id"`
	SharedIps Num    `json:"shared_ips"`
	IpsOurs   Num    `json:"ips_ours"`
	IpsTheirs Num    `json:"ips_theirs"`
}

func (v *OverlapPeer) UnmarshalJSON(d []byte) error {
	type plain OverlapPeer
	return jsnum.DecodeNA(d, (*plain)(v))
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
	// BrowserOverall is each engine's overall browser score of the network
	// (TARGET-SCORES-1); nil in a snapshot from before it.
	BrowserOverall map[string]BrowserOverall `json:"browser_overall"`
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
