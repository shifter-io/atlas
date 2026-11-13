package build

import (
	"strings"

	"github.com/Zeus-Development/proxy-index/api/internal/model"
)

// The N/A report's reasons (/v1/coverage, COVERAGE-REASONS-1): each gap's
// reason code, from the page answers' set (api/answers/README.md, "Missing
// data"), and the source that would fill it: a site.* catalog table or
// column, a framework table, srcSnapshot (the framework's published
// calculation) or none (null).

// naReason is a gap's reason code and source ("" for none).
type naReason struct{ code, source string }

// sourceOrNull is the source as the report serves it: null for none.
func (r naReason) sourceOrNull() *string {
	if r.source == "" {
		return nil
	}
	s := r.source
	return &s
}

const srcSnapshot = "snapshot"

// The sources a gap may name (the na_source code set).
var naSources = codes(srcSnapshot, "The framework's published calculation.",
	"site.providers.hq", "The catalog's headquarters of the network.",
	"site.providers.founded", "The catalog's founding year of the network.",
	"site.providers.website", "The catalog's website of the network.",
	"site.providers.best_for", "The catalog's best uses of the network.",
	"site.pricing", "The catalog's pricing row of the network and kind.",
	"site.pricing.price_per_gb", "The catalog's entry list rate per GB.",
	"site.pricing.price_per_gb_at_scale", "The catalog's lowest list rate per GB.",
	"site.pricing.min_commit", "The catalog's minimum commitment.",
	"site.pricing.trial", "The catalog's trial.",
	"site.pricing_plans", "The catalog's plans.",
	"site.pricing_history", "The catalog's price history.",
	"site.trust_signals.claimed_pool_size", "The network's own claim of its pool size.",
	"site.trust_signals.blacklist_hit_rate", "The catalog's blocklist hit rate.",
	"site.trust_signals.stickiness_hold_rate", "The catalog's sticky session hold rate.",
	"site.trust_signals.leak_checks", "The catalog's leak checks.",
	"site.trust_signals.kyc_verified", "The catalog's identity check of customers.",
	"site.trust_signals.consent_disclosed", "The catalog's disclosure of exit owners' consent.",
	"site.reviews", "The published reviews.",
	"site.targets.category", "The catalog's category of the target.",
	"site.targets.difficulty", "The catalog's difficulty of the target.",
	"site.score_weights", "The catalog's score weights.",
	"site.scopes", "The catalog's test scope row.",
	"public.targets", "The framework's target list.")

func snapshotReason(code string) naReason { return naReason{code, srcSnapshot} }

// catalogOf is a gap a catalog column (or row) would fill.
func catalogOf(col string) naReason { return naReason{NACatalog, col} }

// notMeasuredBy is a gap no test fills and col would: the framework runs no
// such check (trust), collects no such history or lists no such target.
func notMeasuredBy(col string) naReason { return naReason{NANotMeasured, col} }

var (
	notMeasuredSnap  = snapshotReason(NANotMeasured)
	notPublishedSnap = snapshotReason(NANotPublished)
	insufficientSnap = snapshotReason(NAInsufficient)
	noRunSnap        = snapshotReason(NANoRun)
	noEvidenceSnap   = snapshotReason(NANoEvidence)
	noLoadTestSnap   = snapshotReason(NANoLoadTest)
	noScoreToCompare = snapshotReason(NANoScoreToCompare)
	// noBasisPrice: no price_per_gb_basis evidence, so the snapshot has no
	// basis_per_gb (COST-BASIS-200GB-1).
	noBasisPrice = snapshotReason(NACatalog)
	// unknownGap is a gap no rule explains.
	unknownGap = naReason{NAInsufficient, ""}
)

// reasons are the reasons of known gaps; keys are patterns (see pattern()).
var reasons = map[string]naReason{
	"hq":                                   catalogOf("site.providers.hq"),
	"founded":                              catalogOf("site.providers.founded"),
	"website":                              catalogOf("site.providers.website"),
	"bestFor":                              catalogOf("site.providers.best_for"),
	"productMeasurements":                  notMeasuredSnap,
	"pool.claimedPoolSize":                 catalogOf("site.trust_signals.claimed_pool_size"),
	"pool.requestsPerIp":                   notPublishedSnap,
	"pool.uniqueIps24h":                    noRunSnap,
	"pool.uniqueIps30d":                    noRunSnap,
	"pool.churnRate":                       insufficientSnap,
	"pool.recurringShare":                  insufficientSnap,
	"performance.successRateDelta24h":      notPublishedSnap,
	"performance.successRateDelta30d":      notPublishedSnap,
	"performance.successScoreDelta24h":     noScoreToCompare,
	"performance.successScoreDelta30d":     noScoreToCompare,
	"performance.latencyDelta24h":          notPublishedSnap,
	"performance.latencyP50":               noLoadTestSnap,
	"performance.latencyMean":              noLoadTestSnap,
	"performance.latencyP90":               noLoadTestSnap,
	"performance.latencyP99":               noLoadTestSnap,
	"performance.ttfbP50":                  {NANotMeasured, ""},
	"performance.downtimeMinutes24h":       noEvidenceSnap,
	"performance.bandwidthMb24h":           notMeasuredSnap,
	"performance.uptimePct":                noEvidenceSnap,
	"performance.captchaRate":              insufficientSnap,
	"performance.blockRate":                insufficientSnap,
	"pricing.model":                        catalogOf("site.pricing"),
	"pricing.pricePerGb":                   catalogOf("site.pricing.price_per_gb"),
	"pricing.pricePerGbAtScale":            catalogOf("site.pricing.price_per_gb_at_scale"),
	"pricing.minCommit":                    catalogOf("site.pricing.min_commit"),
	"pricing.trial":                        catalogOf("site.pricing.trial"),
	"pricing.plans":                        catalogOf("site.pricing_plans"),
	"pricing.avgPayloadKb":                 notMeasuredSnap,
	"pricing.headlinePriceRank":            catalogOf("site.pricing.price_per_gb"),
	"pricing.priceBasis":                   noBasisPrice,
	"pricing.basisPlanGb":                  noBasisPrice,
	"trust.residentialAuthenticity":        notMeasuredSnap,
	"trust.datacenterAsnShare":             notMeasuredSnap,
	"trust.claimGap":                       notPublishedSnap,
	"trust.blacklistHitRate":               notMeasuredBy("site.trust_signals.blacklist_hit_rate"),
	"trust.stickinessHoldRate":             notMeasuredBy("site.trust_signals.stickiness_hold_rate"),
	"trust.leakChecks":                     notMeasuredBy("site.trust_signals.leak_checks"),
	"trust.kycVerified":                    catalogOf("site.trust_signals.kyc_verified"),
	"trust.consentDisclosed":               catalogOf("site.trust_signals.consent_disclosed"),
	"targets[].successRate":                notMeasuredSnap,
	"targets[].captchaRate":                notMeasuredSnap,
	"targets[].blockRate":                  notMeasuredSnap,
	"targets[].latencyP50":                 notMeasuredSnap,
	"targets[].latencyP90":                 notMeasuredSnap,
	"targets[].delta24h":                   notPublishedSnap,
	"targets[].pageLoadP50":                notMeasuredSnap,
	"targets[].pageLoadP90":                notMeasuredSnap,
	"targets[].engine":                     notMeasuredSnap,
	"targets[].sources[].successRate":      notMeasuredSnap,
	"series.pricePerGb90d":                 notMeasuredBy("site.pricing_history"),
	"series.latency24h":                    noRunSnap,
	"series.success24h":                    noRunSnap,
	"series.uniqueIps24h":                  noRunSnap,
	"series.latency30d":                    noRunSnap,
	"series.success30d":                    noRunSnap,
	"series.uniqueIps30d":                  noRunSnap,
	"ipSamples":                            {NANotPublished, ""},
	"incidents":                            notPublishedSnap,
	"incidents[].troughSuccessRate":        notPublishedSnap,
	"incidents[].impact":                   noEvidenceSnap,
	"geo":                                  notMeasuredSnap,
	"geo[].latencyP50":                     notMeasuredSnap,
	"geo[].successRate":                    notMeasuredSnap,
	"asns":                                 notMeasuredSnap,
	"overlap.exclusiveShare":               insufficientSnap,
	"overlap.sharedShare":                  insufficientSnap,
	"overlap.overlaps":                     insufficientSnap,
	"overlap.overlaps[].requestsBothSides": notPublishedSnap,
	"overlap.likelyUpstream":               {NANotMeasured, ""},
	"score.composite":                      insufficientSnap,
	"score.rank":                           insufficientSnap,
	"score.previousRank":                   noScoreToCompare,
	"score.grade":                          insufficientSnap,
	"reviews.mean":                         catalogOf("site.reviews"),
	"reviews.verifiedMean":                 catalogOf("site.reviews"),
	"seed":                                 {NANotApplicable, ""},
	"runId":                                notMeasuredSnap,
	"collectorVersion":                     notPublishedSnap,
	"category":                             catalogOf("site.targets.category"),
	"engine":                               notMeasuredBy("public.targets"),
	"difficulty":                           catalogOf("site.targets.difficulty"),
}

// reasonPrefixes are the reasons of the gaps under a path prefix, in order.
var reasonPrefixes = []struct {
	prefix string
	reason naReason
}{
	{"targets[].outcomes.", notMeasuredSnap},
	{"targets[].sources[].outcomes.", notMeasuredSnap},
	{"targets[].engines.", noRunSnap},
	{"score.breakdown.", insufficientSnap},
	{"snapshot.", notPublishedSnap},
	{"totals.", notMeasuredSnap},
	{"weights.", catalogOf("site.score_weights")},
}

func reasonFor(path string) naReason {
	p := pattern(path)
	if r, ok := reasons[p]; ok {
		return r
	}
	if r, ok := scoreReason(p); ok {
		return r
	}
	for _, rp := range reasonPrefixes {
		if strings.HasPrefix(p, rp.prefix) {
			return rp.reason
		}
	}
	return unknownGap
}

// scoreReason is the reason of a null browser score (TARGET-SCORES-1): a
// whole scores object (not measured), or one metric's or an overall's score,
// rank or rankedOf, whose status says why (insufficient_data here).
func scoreReason(p string) (naReason, bool) {
	switch {
	case isScoreDelta(p):
		return noScoreToCompare, true
	case strings.HasPrefix(p, "browserOverall."), strings.HasPrefix(p, "successScore."):
		return insufficientSnap, true
	case !strings.HasPrefix(p, "targets[]") || !strings.Contains(p, "scores"):
		return naReason{}, false
	case strings.HasSuffix(p, ".scores"):
		return notMeasuredSnap, true
	}
	return insufficientSnap, true
}

// scopeReason is the reason of a scope's gap: its own code (naReason,
// naReasons: meta_scopes.go), a catalog one the site.scopes row's, any
// other the snapshot's.
func scopeReason(s model.TestScope) func(string) naReason {
	return func(p string) naReason {
		code, ok := s.NAReasons[p]
		switch {
		case !ok && s.NAReason == "":
			return reasonFor(p)
		case !ok:
			code = s.NAReason
		}
		if code == NACatalog {
			return catalogOf("site.scopes")
		}
		return snapshotReason(code)
	}
}

// coveragePaths are the N/A report's paths of a gap's key (reason, source),
// as code sets list them.
func coveragePaths(key string) []string {
	out := []string{"/v1/coverage fields.<id>." + key, "/v1/coverage/{slug} sections.<id>[]." + key}
	for _, e := range []string{"dataset", "targets[]", "scopes[]", "profiles[]"} {
		out = append(out, "/v1/coverage "+e+".sections.fields[]."+key)
	}
	return out
}

func init() {
	RegisterCodeSet(CodeSet{Name: "na_source", Source: CodesFromAPI, UsedIn: coveragePaths("source"), Values: naSources})
}
