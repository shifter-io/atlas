package model

import "github.com/Zeus-Development/proxy-index/api/internal/jsnum"

// Target engines (public.targets.engine).
const (
	EngineHTTP    = "http"
	EngineBrowser = "browser"
)

type TargetMeta struct {
	ID            string `json:"id"`
	Label         Str    `json:"label"`
	Category      Str    `json:"category"`
	PassCondition Str    `json:"passCondition"`
	Difficulty    Num    `json:"difficulty"`
	Source        string `json:"source"`
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
	SuccessRate Num    `json:"successRate"`
	CaptchaRate Num    `json:"captchaRate"`
	BlockRate   Num    `json:"blockRate"`
	LatencyP50  Num    `json:"latencyP50"`
	LatencyP90  Num    `json:"latencyP90"`
	Requests    Num    `json:"requests"`
	Delta24h    Num    `json:"delta24h"`
	// *BrowserBenchmark is set exactly on browser targets; when it is nil its
	// fields are not written, so a network target's entry is unchanged.
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
}

// Outcomes are shares of the completed tries per outcome; they sum to 1.
type Outcomes struct {
	Ok         Num `json:"ok"`
	Challenge  Num `json:"challenge"`
	Block      Num `json:"block"`
	LoginWall  Num `json:"loginWall"`
	Timeout    Num `json:"timeout"`
	ProxyError Num `json:"proxyError"`
	NoContent  Num `json:"noContent"`
	NoAnswer   Num `json:"noAnswer"`
	OtherError Num `json:"otherError"`
}

// TargetSource is one source of a group target.
type TargetSource struct {
	Target      string   `json:"target"`
	Label       Str      `json:"label"`
	SuccessRate Num      `json:"successRate"`
	Requests    Num      `json:"requests"`
	Outcomes    Outcomes `json:"outcomes"`
}
