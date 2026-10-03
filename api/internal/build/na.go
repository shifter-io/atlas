package build

import (
	"bytes"
	"encoding/json"
	"regexp"
	"sort"
	"strings"
)

// Paths whose null is part of the contract rather than missing data
// (patterns: array positions written as [], product categories as *).
var contractNull = map[string]bool{
	"measurement":            true, // raw snapshot product: null for fixtures
	"framework":              true, // framework identity: null for fixtures
	"overlap.likelyUpstream": true, // fixtures: no reseller signature (measured providers declare it N/A)
	"pricing.plans[].gb":     true, // per-IP plans have no GB allowance
	"pricing.plans[].ips":    true, // per-GB plans have no IP count
	"at":                     true, // tape: only measured events carry an ISO stamp
	"invoiceMonth":           true, // reviews: only invoice-verified reviews have one
	"linkedRunId":            true,
	"providerReply":          true,
	"rejectionReason":        true, // community runs: only rejected runs
	"overlapShare":           true, // only rotating products
	"providerSlug":           true, // changelog: fleet-wide entries have no provider
	"providerName":           true,
	"delta":                  true,
}

// Array fields whose elements are named by a key in N/A paths, so a gap reads
// "targets[google_search].delta24h" instead of a position.
var identityKey = map[string]string{"targets": "target", "products": "kind"}

var bracket = regexp.MustCompile(`\[[^\]]*\]`)

// pattern normalises a concrete path for contract and reason lookups.
func pattern(path string) string {
	p := bracket.ReplaceAllString(path, "[]")
	if strings.HasPrefix(p, "productMeasurements.") {
		parts := strings.SplitN(p, ".", 3)
		if len(parts) == 3 {
			return parts[2]
		}
		return "productMeasurements.*"
	}
	return p
}

type naWalker struct {
	paths map[string]bool
	// emptyIsNA marks empty arrays as missing (measured providers: an empty
	// list there means "not collected", never "none").
	emptyIsNA bool
	skip      map[string]bool
}

func (w *naWalker) walk(v any, path, field string) {
	if w.skip[path] {
		return
	}
	switch x := v.(type) {
	case nil:
		if !contractNull[pattern(path)] {
			w.paths[path] = true
		}
	case map[string]any:
		for k, child := range x {
			if k == "na" {
				continue
			}
			p := k
			if path != "" {
				p = path + "." + k
			}
			w.walk(child, p, k)
		}
	case []any:
		if len(x) == 0 && w.emptyIsNA {
			w.paths[path] = true
		}
		for _, child := range x {
			label := ""
			if key, ok := identityKey[field]; ok {
				if m, ok := child.(map[string]any); ok {
					if id, ok := m[key].(string); ok {
						label = id
					}
				}
			}
			w.walk(child, path+"["+label+"]", "")
		}
	}
}

// decode turns a value into generic JSON.
func decode(v any) (any, error) {
	b, err := json.Marshal(v)
	if err != nil {
		return nil, err
	}
	dec := json.NewDecoder(bytes.NewReader(b))
	dec.UseNumber()
	var out any
	err = dec.Decode(&out)
	return out, err
}

// lookup resolves a dotted path (no arrays) in generic JSON.
func lookup(v any, path string) (any, bool) {
	for _, seg := range strings.Split(path, ".") {
		m, ok := v.(map[string]any)
		if !ok {
			return nil, false
		}
		if v, ok = m[seg]; !ok {
			return nil, false
		}
	}
	return v, true
}

func emptyValue(v any) bool {
	switch x := v.(type) {
	case nil:
		return true
	case []any:
		return len(x) == 0
	case map[string]any:
		return len(x) == 0
	}
	return false
}

// naOf lists the N/A paths of one entity: every null leaf that is not null by
// contract, every declared path whose value is empty, and (emptyIsNA) every
// empty list.
func naOf(entity any, declared []string, emptyIsNA bool, skip ...string) ([]string, error) {
	g, err := decode(entity)
	if err != nil {
		return nil, err
	}
	w := &naWalker{paths: map[string]bool{}, emptyIsNA: emptyIsNA, skip: map[string]bool{}}
	for _, s := range skip {
		w.skip[s] = true
	}
	w.walk(g, "", "")
	for _, d := range declared {
		if v, ok := lookup(g, d); ok && emptyValue(v) {
			w.paths[d] = true
		}
	}
	out := make([]string, 0, len(w.paths))
	for p := range w.paths {
		out = append(out, p)
	}
	sort.Strings(out)
	return out, nil
}

// withoutPublishedEmpties drops the N/A mark naOf gives a measured provider's
// empty incident list when its snapshot product publishes outages (v2): there
// an empty list means no outage, a real zero, not "not collected".
func withoutPublishedEmpties(e *entry) []string {
	published := map[string]bool{}
	for k, sp := range e.Snap {
		if sp != nil && sp.Outages != nil {
			published["productMeasurements."+k+".incidents"] = true
			if k == e.P.PrimaryKind {
				published["incidents"] = true
			}
		}
	}
	out := e.P.NA[:0]
	for _, p := range e.P.NA {
		if !published[p] {
			out = append(out, p)
		}
	}
	return out
}

// reasons explains known gaps; keys are patterns (see pattern()).
var reasons = map[string]string{
	"tagline":                              "Editorial field: no site.providers.tagline",
	"hq":                                   "Editorial field: no site.providers.hq",
	"founded":                              "Editorial field: no site.providers.founded",
	"claim":                                "Editorial field: no site.providers.claim (the marketing claim being checked)",
	"website":                              "Editorial field: no site.providers.website",
	"bestFor":                              "Editorial field: no site.providers.best_for",
	"strengths":                            "No site.providers.strengths and no published snapshot to derive them from",
	"weaknesses":                           "No site.providers.weaknesses and no published snapshot to derive them from",
	"verdict":                              "No site.providers.verdict and no published snapshot to derive one from",
	"productMeasurements":                  "No published snapshot product for this provider",
	"pool.claimedPoolSize":                 "No site.trust_signals.claimed_pool_size for this category",
	"pool.requestsPerIp":                   "Not published by the framework snapshot",
	"pool.uniqueIps24h":                    "No published snapshot window (24h)",
	"pool.uniqueIps30d":                    "No published snapshot window (30d)",
	"pool.churnRate":                       "Snapshot pool.churn_rate is null (needs an earlier window to compare)",
	"pool.recurringShare":                  "Snapshot pool.recurring_share is null",
	"performance.successRateDelta24h":      "Not mapped: the snapshot's success_delta is per window (see /v1/providers/{slug} windows)",
	"performance.successRateDelta30d":      "Not mapped: the snapshot's success_delta is per window (see /v1/providers/{slug} windows)",
	"performance.latencyDelta24h":          "Not published by the framework snapshot",
	"performance.latencyP50":               "No latency under load in the 30d window (no load test; the snapshot's under_load.reason says why)",
	"performance.latencyP90":               "No latency under load in the 30d window (no load test; the snapshot's under_load.reason says why)",
	"performance.latencyP99":               "No latency under load in the 30d window (no load test; the snapshot's under_load.reason says why)",
	"performance.ttfbP50":                  "TTFB is not measured by the framework",
	"performance.downtimeMinutes24h":       "No time-based uptime in the snapshot's 24h window (no outage-time basis or no country tested)",
	"performance.bandwidthMb24h":           "Not mapped: wire bytes are in measurement.windows.*.metrics.bytes_down",
	"performance.uptimePct":                "Snapshot metrics.uptime is null (no finished pulse batches, or a v1 snapshot)",
	"performance.captchaRate":              "Snapshot metrics.challenge_rate is null (no outcome-classified requests, or a v1 snapshot)",
	"performance.blockRate":                "Snapshot metrics.block_rate is null (no outcome-classified requests, or a v1 snapshot)",
	"pricing.model":                        "No site.pricing row for this provider and category",
	"pricing.pricePerGb":                   "No entry list rate: snapshot products[].pricing.entry_per_gb and site.pricing.price_per_gb are null",
	"pricing.pricePerGbAtScale":            "No lowest list rate: snapshot products[].pricing.lowest_per_gb and site.pricing.price_per_gb_at_scale are null",
	"pricing.minCommit":                    "No site.pricing.min_commit",
	"pricing.trial":                        "No site.pricing.trial",
	"pricing.plans":                        "No site.pricing_plans rows",
	"pricing.costPer1kSuccess":             "Needs a list rate plus the snapshot's wire bytes and request success rate (or site.pricing avg_payload_kb)",
	"pricing.avgPayloadKb":                 "No site.pricing.avg_payload_kb",
	"pricing.costBasis":                    "No site.pricing.cost_basis and no derivable cost",
	"pricing.headlinePriceRank":            "No site.pricing.headline_price_rank",
	"pricing.valueRank":                    "No site.pricing.value_rank",
	"trust.residentialAuthenticity":        "Needs an every-request snapshot's pool.geo_accuracy (exit IPs in the requested country) or site.trust_signals.residential_authenticity",
	"trust.datacenterAsnShare":             "Snapshot pool.datacenter_asn_share is null and no catalog value",
	"trust.claimGap":                       "Not published by every-request snapshots (and pool.datacenter_asn_share is null in older ones), no catalog value",
	"trust.blacklistHitRate":               "Blocklist checks are not run by the framework",
	"trust.stickinessHoldRate":             "Sticky sessions are not tested by the framework",
	"trust.stickinessAdvertised":           "No site.trust_signals.stickiness_advertised",
	"trust.leakChecks":                     "Leak checks are not run by the framework",
	"trust.kycVerified":                    "No site.trust_signals.kyc_verified",
	"trust.consentDisclosed":               "No site.trust_signals.consent_disclosed",
	"targets[].successRate":                "Target not measured for this product in the published snapshot",
	"targets[].captchaRate":                "Target not measured, or no outcome classification (v1 snapshot)",
	"targets[].blockRate":                  "Target not measured, or no outcome classification (v1 snapshot)",
	"targets[].latencyP50":                 "Target not measured, or no load test for its latency under load in the window",
	"targets[].latencyP90":                 "Target not measured, or no load test for its latency under load in the window",
	"targets[].delta24h":                   "Per-target 24h deltas are not published by the framework snapshot",
	"series.pricePerGb90d":                 "Price history is not collected",
	"series.latency24h":                    "No completed 5-minute buckets in the 24h window",
	"series.success24h":                    "No completed 5-minute buckets in the 24h window",
	"series.uniqueIps24h":                  "No completed 5-minute buckets in the 24h window",
	"series.latency30d":                    "No completed buckets in the 30d window",
	"series.success30d":                    "No completed buckets in the 30d window",
	"series.uniqueIps30d":                  "No completed buckets in the 30d window",
	"ipSamples":                            "Exit-IP samples are not published for measured providers",
	"incidents":                            "The snapshot publishes no outages (v1 snapshot)",
	"incidents[].troughSuccessRate":        "Outages do not publish a trough success rate",
	"incidents[].impact":                   "No countries tested in the snapshot's 30d uptime, so an outage has no capacity weight",
	"geo":                                  "No country breakdown in the published snapshot",
	"geo[].latencyP50":                     "No per-country breakdown (snapshot v1 or no country rows)",
	"geo[].successRate":                    "No per-country breakdown (snapshot v1 or no country rows)",
	"asns":                                 "No ASN breakdown in the published snapshot",
	"overlap.exclusiveShare":               "Snapshot overlap.exclusive_share is null (no peer products in the cohort)",
	"overlap.sharedShare":                  "Snapshot overlap.shared_share is null (no peer products in the cohort)",
	"overlap.overlaps":                     "No peer product shares this cohort in the published snapshot",
	"overlap.overlaps[].requestsBothSides": "Not published by the framework snapshot",
	"overlap.likelyUpstream":               "Reseller detection is not computed for measured providers",
	"score.composite":                      "No ready balanced score in the published snapshot",
	"score.rank":                           "No ready balanced score in the published snapshot",
	"score.previousRank":                   "No previous ranked score",
	"score.grade":                          "No ready balanced score in the published snapshot",
	"reviews.mean":                         "No published reviews",
	"reviews.verifiedMean":                 "No verified reviews",
	"seed":                                 "Only simulated datasets have a seed",
	"runId":                                "No published snapshot and no fixture run id",
	"collectorVersion":                     "No published snapshot implementation id and no fixture collector version",
	"category":                             "No site.targets.category for this framework target",
	"difficulty":                           "No site.targets.difficulty for this framework target",
}

func reasonFor(path string) string {
	p := pattern(path)
	if r, ok := reasons[p]; ok {
		return r
	}
	if strings.HasPrefix(p, "score.breakdown.") {
		return "Score component unavailable in the published snapshot"
	}
	if strings.HasPrefix(p, "snapshot.") {
		return "No published framework calculation (public.current_calculation is empty)"
	}
	if strings.HasPrefix(p, "totals.") {
		return "Fleet total needs fixture or measured inputs that are missing"
	}
	if strings.HasPrefix(p, "weights.") {
		return "No weight in the snapshot methodology or site.score_weights"
	}
	return "No value in the database for this field"
}

// CoverageField is one N/A path with its explanation.
type CoverageField struct {
	Path   string `json:"path"`
	Reason string `json:"reason"`
}

// CoverageEntity is the N/A report of one entity.
type CoverageEntity struct {
	ID       string                     `json:"id"`
	Name     string                     `json:"name,omitempty"`
	Source   string                     `json:"source,omitempty"`
	NACount  int                        `json:"naCount"`
	Sections map[string][]CoverageField `json:"sections"`
}

// Coverage is /v1/coverage.
type Coverage struct {
	GeneratedAt string                      `json:"generatedAt"`
	BuiltAt     string                      `json:"builtAt"`
	Summary     CoverageSummary             `json:"summary"`
	Dataset     CoverageEntity              `json:"dataset"`
	Providers   []CoverageEntity            `json:"providers"`
	Targets     []CoverageEntity            `json:"targets"`
	Scopes      []CoverageEntity            `json:"scopes"`
	Profiles    []CoverageEntity            `json:"profiles"`
	Records     map[string]CoverageRecords  `json:"records"`
	Fields      map[string]CoverageFieldUse `json:"fields"`
}

type CoverageSummary struct {
	Providers         int            `json:"providers"`
	ProvidersBySource map[string]int `json:"providersBySource"`
	NAFields          int            `json:"naFields"`
	NAFieldsBySource  map[string]int `json:"naFieldsBySource"`
	SnapshotPublished bool           `json:"snapshotPublished"`
}

// CoverageRecords summarises tape/changelog/reviews/community runs.
type CoverageRecords struct {
	Count   int            `json:"count"`
	NACount int            `json:"naCount"`
	Fields  map[string]int `json:"fields"`
}

// CoverageFieldUse counts, per field pattern, how many providers lack it.
type CoverageFieldUse struct {
	Providers []string `json:"providers"`
	Reason    string   `json:"reason"`
}

func section(path string) string {
	p := bracket.ReplaceAllString(path, "")
	parts := strings.Split(p, ".")
	if parts[0] == "productMeasurements" && len(parts) >= 3 {
		return "productMeasurements." + parts[1] + "." + parts[2]
	}
	switch parts[0] {
	case "pool", "performance", "pricing", "trust", "targets", "series", "ipSamples", "incidents", "geo", "asns", "overlap", "score", "reviews", "productMeasurements", "products":
		return parts[0]
	}
	return "identity"
}

func coverageEntity(id, name, source string, na []string, sectioned bool) CoverageEntity {
	e := CoverageEntity{ID: id, Name: name, Source: source, NACount: len(na), Sections: map[string][]CoverageField{}}
	for _, p := range na {
		s := "fields"
		if sectioned {
			s = section(p)
		}
		e.Sections[s] = append(e.Sections[s], CoverageField{Path: p, Reason: reasonFor(p)})
	}
	return e
}
