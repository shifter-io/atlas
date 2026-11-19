package model

import "github.com/Zeus-Development/atlas/api/internal/jsnum"

// Target engines (public.targets.engine).
const (
	EngineHTTP    = "http"
	EngineBrowser = "browser"
)

type TargetMeta struct {
	ID string `json:"id"`
	// Label and PassCondition are the catalog's English reference text: only
	// /v1/codes serves them (TARGET-TEXT-REMOVE-1); a client words a target by id.
	Label         Str `json:"-"`
	PassCondition Str `json:"-"`
	// Category is a target category code (/v1/codes target_category).
	Category   Str    `json:"category"`
	Difficulty Num    `json:"difficulty"`
	Source     string `json:"source"`
	// Engine is the framework target's engine, EngineHTTP or EngineBrowser
	// (nil for a catalog-only target, which is not measured).
	Engine Str `json:"engine"`
	// Group is the group target this target is a source of (browser pulse:
	// ai_answers), from the published snapshot; nil when it is none. A client
	// shows the targets whose group is nil as cards.
	Group Str      `json:"group"`
	NA    []string `json:"na"`
	// Sources is a group target's source targets, from the published
	// snapshot. Not served here: the group's benchmarks carry them.
	Sources []string `json:"-"`
}

type TargetBenchmark struct {
	Target      string `json:"target"`
	SuccessRate Num    `json:"-"` // internal (SUCCESS-RATE-REMOVE-1, #900): Scores is served
	CaptchaRate Num    `json:"-"` // internal (#900)
	BlockRate   Num    `json:"-"` // internal (#900)
	LatencyP50  Num    `json:"latencyP50"`
	LatencyP90  Num    `json:"latencyP90"`
	Requests    Num    `json:"requests"`
	Delta24h    Num    `json:"delta24h"`
	// Scores is the target's rank-based scores: a browser target's main
	// engine's, a network target's success score (TargetScores.Success only);
	// null when the snapshot has none.
	Scores *TargetScores `json:"scores"`
	// NetworkBlocked is whether the network's gateway refuses the target for
	// our account in the range (api/FIELDS.md "Network-blocked targets");
	// null under 100 tries or when the whole gateway refuses.
	NetworkBlocked Bool `json:"networkBlocked"`
	// *BrowserBenchmark is set exactly on browser targets; when it is nil its
	// fields are not written.
	*BrowserBenchmark
}

func (v *TargetBenchmark) UnmarshalJSON(d []byte) error {
	type plain TargetBenchmark
	return jsnum.DecodeNA(d, (*plain)(v))
}

// BrowserBenchmark is a browser target's card figures. It has no methods: an
// embedded type's JSON methods would be promoted to TargetBenchmark.
type BrowserBenchmark struct {
	Outcomes Outcomes `json:"outcomes"`
	// PageLoadP50 and PageLoadP90 are the page-load times (ms) of the
	// successful tries; browser cards show them instead of latency.
	PageLoadP50 Num `json:"pageLoadP50"`
	PageLoadP90 Num `json:"pageLoadP90"`
	// Sources is a group target's breakdown, one entry per source target in
	// the snapshot's order; nil (not written) on every other target.
	Sources []TargetSource `json:"sources,omitempty"`
	// Engine is the browser engine whose tries make the figures above (the
	// one with the most completed tries); null when the snapshot names none.
	Engine Str `json:"engine"`
	// Engines is each engine's own figures, by engine name (never added
	// together); empty when the snapshot has none.
	Engines map[string]EngineBenchmark `json:"engines"`
}

// EngineBenchmark is one browser engine's figures of a browser target.
type EngineBenchmark struct {
	Requests    Num      `json:"requests"`
	Successes   Num      `json:"successes"`
	Outcomes    Outcomes `json:"outcomes"`
	PageLoadP50 Num      `json:"pageLoadP50"`
	PageLoadP90 Num      `json:"pageLoadP90"`
	// Scores is the engine's rank-based scores; null when the snapshot has none.
	Scores *TargetScores `json:"scores"`
}

// TargetScores are a target's rank-based 1-10 scores in one window and
// engine (TARGET-SCORES-1), next to the raw rates they rank. A network
// target (http) has challenge and block too since CHALLENGE-BLOCK-TIE-1; in
// an older snapshot Challenge and Block are nil, not written.
type TargetScores struct {
	Success   TargetSuccess `json:"success"`
	Challenge *MetricScore  `json:"challenge,omitempty"`
	Block     *MetricScore  `json:"block,omitempty"`
}

// TargetSuccess is a target's success score with Delta, its score minus the
// previous published build's for the same network, range and engine
// (SUCCESS-SCORE-DELTA-1): null when either build has no ranked score.
type TargetSuccess struct {
	MetricScore
	Delta Num `json:"delta"`
}

func (v *TargetSuccess) UnmarshalJSON(d []byte) error {
	type plain TargetSuccess
	return jsnum.DecodeNA(d, (*plain)(v))
}

// MetricScore is one metric's score: Score (1-10), Rank (fractional on a tie)
// and RankedOf (M, the networks ranked) are null unless Status is "ranked".
type MetricScore struct {
	Score    Num    `json:"score"`
	Rank     Num    `json:"rank"`
	RankedOf Num    `json:"rankedOf"`
	Status   string `json:"status"`
}

// OverallScore is a network's overall browser success score for one engine,
// with the targets it averaged and their difficulty weights.
type OverallScore struct {
	MetricScore
	Targets []string       `json:"targets"`
	Weights map[string]Num `json:"weights"`
}

// SuccessScore is a network's overall 1-10 success score in one window over
// every target (SUCCESS-SCORE-API-1): the difficulty-weighted mean of its
// ranked targets' success scores, Targets each one's score and Weights its
// difficulty (1 on an HTTP target), ranked among the networks of its kind.
// Status is "ranked" or "no_ranked_targets".
type SuccessScore struct {
	MetricScore
	Targets map[string]Num `json:"targets"`
	Weights map[string]Num `json:"weights"`
}

// BrowserOverall is a network's overall browser score for one engine.
type BrowserOverall struct {
	Success OverallScore `json:"success"`
}

// Outcomes are shares of the completed tries per outcome; they sum to 1.
type Outcomes struct {
	Ok         Num `json:"-"` // internal (SUCCESS-RATE-REMOVE-1, #900): scores.success is served
	Challenge  Num `json:"challenge"`
	Block      Num `json:"block"`
	LoginWall  Num `json:"loginWall"`
	Timeout    Num `json:"timeout"`
	ProxyError Num `json:"proxyError"`
	NoContent  Num `json:"noContent"`
	NoAnswer   Num `json:"noAnswer"`
	OtherError Num `json:"otherError"`
}

// TargetSource is one source of a group target, by its target id (/v1/targets
// names it).
type TargetSource struct {
	Target      string   `json:"target"`
	SuccessRate Num      `json:"-"` // internal (SUCCESS-RATE-REMOVE-1, #900): Scores is served
	Requests    Num      `json:"requests"`
	Outcomes    Outcomes `json:"outcomes"`
	// Scores is the source target's own rank-based scores in the window (its
	// main engine's, as TargetBenchmark.Scores); null when the snapshot has none.
	Scores *TargetScores `json:"scores"`
}
