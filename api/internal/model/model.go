// Package model is the website's Dataset contract (src/lib/types.ts) in Go.
//
// Every field is always written. A value the API does not have is JSON null
// (Num NaN, nil *string, nil *bool) and its path is listed in the owning
// entity's `na` array. Slices are never nil, so an empty list is [] rather than
// null; when an empty list means "not collected" rather than "none", the
// builder lists it in `na` explicitly.
package model

import "github.com/Zeus-Development/proxy-index/api/internal/jsnum"

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
// only a catalog row is "catalog", an editorial tape row SourceEditorial);
// the demo fixtures' "demo" is gone.
const SourceMeasured = "measured"

// SourceEditorial is the source of a tape row written by hand (site.tape_events).
const SourceEditorial = "editorial"

// VerificationLabels is data.ts verificationLabels: a review's verification
// as the site prints it.
var VerificationLabels = map[string]string{
	"invoice_verified": "Invoice verified",
	"run_verified":     "Run verified",
	"unverified":       "Unverified",
	"pending":          "In review",
}

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
	ID      string    `json:"id"`
	Label   Str       `json:"label"`
	Weights Breakdown `json:"weights"`
	NA      []string  `json:"na"`
}

type ProviderProduct struct {
	Kind     string `json:"kind"`
	Name     Str    `json:"name"`
	Measured bool   `json:"measured"`
	URL      Str    `json:"url"`
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

// StandingRank is every-request.ts kindStanding's rank: the framework's rank,
// or 1 for a network with a ready score alone in its kind (the framework
// leaves it unranked); NaN otherwise.
func StandingRank(rank, score, peers Num) Num {
	if !rank.OK() && score.OK() && peers == 1 {
		return 1
	}
	return rank
}

type TapeEvent struct {
	ID   string `json:"id"`
	Kind string `json:"kind"`
	// Event is the raw event kind (hour_released, ip_overlap, pulse_down, ...)
	// that Kind is the category of; an editorial row's own kind.
	Event        string   `json:"event"`
	ProviderSlug string   `json:"providerSlug"`
	ProviderName string   `json:"providerName"`
	Product      string   `json:"product"`
	Title        string   `json:"-"` // Title, Detail and Value: English text, not served (#962)
	Detail       string   `json:"-"`
	Signal       Num      `json:"signal"`
	Value        string   `json:"-"`
	AgoSeconds   Num      `json:"agoSeconds"`
	At           Str      `json:"at"`
	Source       string   `json:"source"`
	NA           []string `json:"na"`
	// Country, Region, Target and Severity are the event's, as /v1/events
	// serves them; null on an editorial row.
	Country  Str `json:"country"`
	Region   Str `json:"region"`
	Target   Str `json:"target"`
	Severity Str `json:"severity"`
	// Values is the event's numbers (TAPE-VALUES-1); all null on an editorial row.
	Values EventValues `json:"values"`
}

// EventValues is one event's numbers (TAPE-VALUES-1), the values object of
// its tape row and of its /v1/events row. Its keys are fixed per kind
// (api/FIELDS.md, "Tape and event values"): the 8 keys of every row and
// the kind's own; a key the raw data lacks is null.
type EventValues map[string]any

type ChangeEvent struct {
	ID           string   `json:"id"`
	Kind         string   `json:"kind"`
	ProviderSlug Str      `json:"providerSlug"`
	ProviderName Str      `json:"providerName"`
	Delta        Num      `json:"delta"`
	Unit         string   `json:"unit"`
	AgoSeconds   Num      `json:"agoSeconds"`
	Severity     string   `json:"severity"`
	Source       string   `json:"source"`
	NA           []string `json:"na"`
}

type UserReview struct {
	ID           string `json:"id"`
	ProviderSlug string `json:"providerSlug"`
	Author       string `json:"author"`
	Role         string `json:"role"`
	Rating       Num    `json:"rating"`
	Title        string `json:"title"`
	Body         string `json:"body"`
	Product      string `json:"product"`
	UseCase      string `json:"useCase"`
	MonthlySpend string `json:"monthlySpend"`
	MonthsUsed   Num    `json:"monthsUsed"`
	Verification string `json:"verification"`
	// VerificationLabel is Verification as the site prints it (VerificationLabels).
	VerificationLabel string   `json:"verificationLabel"`
	InvoiceMonth      Str      `json:"invoiceMonth"`
	LinkedRunID       Str      `json:"linkedRunId"`
	HelpfulCount      Num      `json:"helpfulCount"`
	ProviderReply     Str      `json:"providerReply"`
	AgoSeconds        Num      `json:"agoSeconds"`
	Source            string   `json:"source"`
	NA                []string `json:"na"`
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
	SuccessScore     *MetricScore      `json:"successScore"` // the run's 1-10 success score (build runScores)
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
