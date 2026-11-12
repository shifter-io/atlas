package build

import (
	"slices"
	"strings"

	"github.com/Zeus-Development/proxy-index/api/internal/model"
)

// The reason codes of a page answer's N/A paths (api/answers/README.md,
// "Missing data"): why a field is null, for a client to read.
const (
	// NACatalog: the value comes from a site.* catalog row that does not exist.
	NACatalog = "catalog"
	// NANotMeasured: the target or figure has no test in production's cadence.
	NANotMeasured = "not_measured"
	// NANoRun: the window has no run of the network or target.
	NANoRun = "no_run_in_window"
	// NANoEvidence: availability without monitoring evidence in the window.
	NANoEvidence = "no_evidence"
	// NAInsufficient: the calculator published null for a reason no other
	// code covers (score_na.go has the score's codes).
	NAInsufficient = "insufficient_data"
	// NANotPublished: exit addresses are not published (ipSamples).
	NANotPublished = "not_published"
	// NANotMeasuredPerPlace: a place row without its own requests or latency:
	// daily tests record requests per requested country, not per exit place.
	NANotMeasuredPerPlace = "not_measured_per_place"
	// NANotApplicable: the field has no meaning for this row (a per-GB plan's
	// IP count, an incident entry's title, a non-incident entry's incident).
	NANotApplicable = "not_applicable"
	// NAFewSuccesses: a target's latency is measured, but no time slot of the
	// window had [slots] min_latency_successes (30) light-load successes.
	NAFewSuccesses = "few_successes"
	// NAHistoryCoversRange: observedDays is null because the history covers
	// the whole range.
	NAHistoryCoversRange = "history_covers_range"
)

// NAList is an answer entity's N/A paths, as every entity's na (every null
// that is not null by contract, and every declared empty list), with a reason
// code each: naReason is the most common one, naReasons each path whose code
// differs (so a body stays small when a whole cell has no run). Both are left
// out when there is nothing to put in them.
type NAList struct {
	NA      []string          `json:"na"`
	Reason  string            `json:"naReason,omitempty"`
	Reasons map[string]string `json:"naReasons,omitempty"`
}

// list sets l from entity's nulls and its declared paths that are empty; code
// gives a path pattern's reason. skip leaves out the paths whose entities
// carry their own na.
func (l *NAList) list(entity any, declared []string, code func(string) string, skip ...string) error {
	return l.listBy(entity, declared, func(p string) string { return code(pattern(p)) }, skip...)
}

// listBy is list with code given each concrete path (a plan's, by its name);
// a successScore's null score, rank and rankedOf have its status.
func (l *NAList) listBy(entity any, declared []string, code func(string) string, skip ...string) error {
	na, status, err := answerNAOf(entity, declared, skip...)
	if err != nil {
		return err
	}
	codes := make([]string, len(na))
	for i, p := range na {
		if c, ok := status[p]; ok {
			codes[i] = c
			continue
		}
		codes[i] = code(p)
	}
	l.set(na, codes)
	return nil
}

// planCodes is the reason of each plan's null amount, by its N/A path: a
// plan without an IP count is sold by GB, a per-IP plan has no GB amount,
// an untiered pool no tier (not_applicable); a plan sold by GB without a GB
// amount lacks it in the catalog.
func planCodes(plans []model.PricingPlan) map[string]string {
	out := map[string]string{}
	for _, pl := range plans {
		at := "pricing.plans[" + model.SV(pl.Name) + "]."
		out[at+"ips"], out[at+"gb"], out[at+"catalog.tier"] = NANotApplicable, NACatalog, NANotApplicable
		if pl.Ips.OK() {
			out[at+"gb"] = NANotApplicable
		}
	}
	return out
}

// set sets l to paths, each with its code: naReason the most common code
// (the first by name on a tie), naReasons the others.
func (l *NAList) set(paths, codes []string) {
	count := map[string]int{}
	for _, c := range codes {
		count[c]++
	}
	l.NA, l.Reason, l.Reasons = paths, "", nil
	for c, n := range count {
		if n > count[l.Reason] || n == count[l.Reason] && c < l.Reason {
			l.Reason = c
		}
	}
	for i, p := range paths {
		if codes[i] != l.Reason {
			if l.Reasons == nil {
				l.Reasons = map[string]string{}
			}
			l.Reasons[p] = codes[i]
		}
	}
}

// ReasonOf is a listed path's reason code ("" when path is not listed).
func (l *NAList) ReasonOf(path string) string {
	if c, ok := l.Reasons[path]; ok {
		return c
	}
	if slices.Contains(l.NA, path) {
		return l.Reason
	}
	return ""
}

// naFacts is what a figure's reason depends on: whether the network (or
// target) is measured at all, has a run in the window, and has a list rate
// and a claimed pool size, and the reasons of its score's nulls.
type naFacts struct {
	measured, ran, priced, claimed bool
	// fewSuccesses: the latency is measured but null (a cell's, fewSuccesses()).
	fewSuccesses bool
	why          scoreWhy
}

// Figures by the reason their null has (keys: a path's figure, see figure).
var (
	// From site.* rows: pricing, trial, trust declarations, editorial text,
	// target meta, profiles and reviews.
	catalogFigures = setOf("listRate", "pricePerGb", "pricePerGbAtScale", "priceLabel", "trial", "trialKind", "hasTrial",
		"kycVerified", "consentDisclosed", "stickinessAdvertised", "tagline", "claimedPoolSize", "claimed", "difficulty", "hardest",
		"passCondition", "category", "label", "profiles", "reviews", "communityRuns", "summary", "schedule", "bill",
		"hq", "founded", "claim", "verdict", "website", "strengths", "weaknesses", "bestFor", "products", "model",
		"minCommit", "plans", "costBasis", "url", "note")
	// A price times a measured figure, or a price's rank: null without a list rate.
	costFigures = setOf("costPer1kSuccess", "costRank", "valueRank", "alternative", "cheapest", "dearest", "spread",
		"spreadX", "median", "best", "worst", "value", "headlinePriceRank")
	// Availability: no value without monitoring evidence.
	evidenceFigures = setOf("uptimePct", "downtimeMinutes", "downtimeMinutes24h", "incidentCount", "incidents")
	// Never tested in production's cadence: latency needs a latency test;
	// TTFB is not tested; the average payload needs the window's wire bytes.
	unmeasuredFigures = setOf("latencyP50", "latencyP90", "latencyP99", "latencyMean", "medianLatency", "fastest",
		"ttfbP50", "avgPayloadKb")
	// Never tested by the framework, run or not: a value comes from a
	// site.trust_signals row only.
	untestedFigures = setOf("blacklistHitRate", "stickinessHoldRate", "leakChecks")
)

func setOf(keys ...string) map[string]bool {
	out := make(map[string]bool, len(keys))
	for _, k := range keys {
		out[k] = true
	}
	return out
}

// figure is the figure a path pattern is of: its last key, the ranked figure
// for a rank's fields ("ranks.listRate.rank" is listRate, "costRank.rank"
// costRank).
func figure(p string) string {
	keys := strings.Split(strings.ReplaceAll(p, "[]", ""), ".")
	last := len(keys) - 1
	for last > 0 && (keys[last] == "rank" || keys[last] == "peers" || keys[last] == "rankedOf") {
		last--
	}
	return keys[last]
}

// code is a figure's reason under f.
func (f naFacts) code(p string) string {
	key := figure(p)
	switch {
	case key == "ipSamples":
		return NANotPublished
	case untestedFigures[key]:
		return NANotMeasured
	case catalogFigures[key]:
		return NACatalog
	case key == "claimShare" && !f.claimed:
		return NACatalog
	case costFigures[key] && !f.priced:
		return NACatalog
	case !f.measured:
		return NANotMeasured
	case !f.ran:
		return NANoRun
	case evidenceFigures[key]:
		return NANoEvidence
	case f.fewSuccesses && (key == "latencyP50" || key == "latencyP90"):
		return NAFewSuccesses
	case unmeasuredFigures[key]:
		return NANotMeasured
	case key == "observedDays":
		return NAHistoryCoversRange
	}
	if c := f.why.code(p); c != "" {
		return c
	}
	return NAInsufficient
}

// listNA lists the N/A paths of every ranking row and answer and of every
// network answer's profile and range stats, once their figures are set.
func (pg *Pages) listNA(entries []*entry) error {
	for _, set := range []map[string]*RankingsAnswer{pg.Rankings, pg.RankingsByKind} {
		for _, ans := range set {
			if err := ans.listNA(); err != nil {
				return err
			}
		}
	}
	bySlug := entriesBySlug(entries)
	for _, ans := range pg.Network {
		if err := ans.listNA(bySlug[ans.Network.Slug]); err != nil {
			return err
		}
	}
	return nil
}

func (a *RankingsAnswer) listNA() error {
	priced := false
	for i := range a.Networks {
		r := &a.Networks[i]
		priced = priced || r.ListRate.OK()
		if err := r.NAList.list(r, nil, r.facts().code); err != nil {
			return err
		}
	}
	f := naFacts{measured: true, ran: true, priced: priced}
	return a.NAList.list(a, []string{"profiles"}, f.code, "networks")
}

// facts is a ranking row's naFacts.
func (r *RankingRow) facts() naFacts {
	return naFacts{measured: r.Source == model.SourceMeasured, ran: r.Requests.OK() && r.Requests > 0, priced: r.ListRate.OK(), why: r.why}
}

// listNA lists a network answer's own N/A paths, its profile's and each
// range's (e: its network's entry; nil for the all-networks answer, whose
// fleet figures are measured).
func (a *NetworkAnswer) listNA(e *entry) error {
	f := naFacts{measured: e != nil && e.Snap[a.Kind] != nil || a.Kind == AllNetworks, why: headWhy(e)}
	if p := a.Profile; p != nil {
		f.priced, f.claimed = p.Pricing.ListRate.OK(), p.Pool.ClaimedPoolSize.OK()
		plans, code := planCodes(p.Pricing.Plans), a.windowCode(f, profileRange)
		byPath := func(path string) string {
			if c, ok := plans[path]; ok {
				return c
			}
			return code(pattern(path))
		}
		if err := p.NAList.listBy(p, profileEmpties(e, a.Kind), byPath); err != nil {
			return err
		}
	}
	for _, s := range a.Stats {
		sf := f
		sf.ran = s.Requests.OK() && s.Requests > 0
		sf.why = scoreWhy{}
		if err := s.NAList.list(s, nil, sf.code); err != nil {
			return err
		}
	}
	code := a.windowCode(f, totalsRange)
	byKind := func(p string) string {
		if strings.HasPrefix(p, "changelog[].") {
			// Null by the entry's kind (the website's ChangeEntry): the incident
			// block or the title, detail and delta; an ongoing incident's end figures.
			return NANotApplicable
		}
		if a.Kind == AllNetworks && strings.HasSuffix(p, "successScore") {
			return NANotApplicable // the networks' scores are ranks among them: none for all of them
		}
		return code(p)
	}
	return a.NAList.list(a, []string{"reviews", "communityRuns"}, byKind, "profile", "stats", "reviews[]",
		"communityRuns[]", "tape[]")
}

// windowCode is the reason code of a network answer's path under f, whose
// window (rangeOf: "" for none) has a run when its totals have requests: a
// window without one gives its nulls no_run_in_window (NO-RUN-WINDOW-1).
func (a *NetworkAnswer) windowCode(f naFacts, rangeOf func(string) string) func(string) string {
	return func(p string) string {
		wf := f
		wf.ran = f.measured
		if r := rangeOf(p); r != "" {
			t := a.Ranges[r]
			wf.ran = wf.ran && t != nil && t.Requests.OK() && t.Requests > 0
		}
		if wf.ran && strings.Contains(p, ".countries[].") {
			return NANotMeasuredPerPlace // an exit country no test requested
		}
		return wf.code(p)
	}
}

// totalsRange is the range of a network answer path under ranges.<range>.
func totalsRange(p string) string {
	if r, ok := strings.CutPrefix(p, "ranges."); ok {
		return strings.SplitN(r, ".", 2)[0]
	}
	return ""
}

// profileRange is the range of a profile path's figure: 24h for a 24h
// figure (pool.uniqueIps24h, performance.requests24h), else 30d, the window
// of the profile's measured blocks.
func profileRange(p string) string {
	if strings.HasSuffix(figure(p), "24h") {
		return "24h"
	}
	return "30d"
}

// profileEmpties is the provider's N/A paths a kind's profile may hold as an
// empty list that means "not collected" (the provider's na lists them with
// emptyIsNA): the kind's productMeasurements.<kind>.* paths without their
// prefix, and its other paths. naOf keeps those that resolve to an empty
// value in the profile (ipSamples, pricing.plans, bestFor, trust.leakChecks).
func profileEmpties(e *entry, kind string) []string {
	if e == nil {
		return nil
	}
	out, prefix := []string{}, "productMeasurements."+kind+"."
	for _, p := range e.P.NA {
		switch {
		case strings.HasPrefix(p, prefix):
			out = append(out, strings.TrimPrefix(p, prefix))
		case !strings.HasPrefix(p, "productMeasurements."):
			out = append(out, p)
		}
	}
	return out
}

// listNA lists the N/A paths of the home's networks (rows: their ranking
// rows, in order) and its own.
func (a *HomeAnswer) listNA(rows []RankingRow) error {
	priced := false
	for i := range a.Networks {
		n := &a.Networks[i]
		f := rows[i].facts()
		f.claimed = n.Claimed.OK()
		priced = priced || f.priced
		if err := n.NAList.list(n, nil, f.code); err != nil {
			return err
		}
	}
	f := naFacts{measured: true, ran: true, priced: priced}
	code := func(p string) string {
		if p == "insights.hardestRates" && a.Insights.Hardest != nil {
			return NANoRun // the hardest target has no network's success in the range
		}
		return f.code(p)
	}
	return a.NAList.list(a, []string{"profiles", "insights.hardestRates"}, code, "networks", "tape[]", "profiles[]")
}

// listNA lists the N/A paths of the overlap's networks and its market. Every
// range's pairs are computed (24h every build, 7d and 30d once a UTC day): a
// network with no run in the range has no_run_in_window, other nulls
// insufficient_data.
func (a *OverlapAnswer) listNA() error {
	insufficient := func(string) string { return NAInsufficient }
	for i := range a.Networks {
		n, code := &a.Networks[i], insufficient
		if !n.ran {
			code = func(string) string { return NANoRun }
		}
		if err := n.NAList.list(n, []string{"peers"}, code); err != nil {
			return err
		}
	}
	return a.NAList.list(a, nil, insufficient, "networks")
}
