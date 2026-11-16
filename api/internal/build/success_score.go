package build

import (
	"math"
	"slices"
	"sort"
	"strings"

	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/snap"
)

// Success scores (SUCCESS-SCORE-API-1): the 1-10 scores the website shows in
// place of success rates. A target's and a network's overall score are the
// calculator's (atlas calculated/target_scores.py); a place row's is
// computed here with the same rule (placeScores), and the market's is
// absolute (marketScore): the networks' scores are ranks, whose median is
// always about 5.5.

// The rank rule of calculated/target_scores.py (metric_scores): a network is
// ranked with at least scoreMinRequests requests, a metric with at least
// scoreMinRanked ranked networks, unless their best and worst rates are
// within the larger of scoreTightPts points and scoreTightZ standard errors.
const (
	scoreMinRequests = 100
	scoreMinRanked   = 3
	scoreTightPts    = 3.0
	scoreTightZ      = 1.96
)

// Score statuses (calculated/target_scores.py).
const (
	scoreRanked          = "ranked"
	scoreTooFewNetworks  = "too_few_networks"
	scoreNoRankedTargets = "no_ranked_targets"
	// An HTTP target's success score when every request it has in the range
	// is from a country with too few IPs (SUCCESS-MIN-IPS-1).
	scoreTooFewIPs = "too_few_ips"
)

// successScore is a window's overall success score; nil (null) when the
// snapshot has none.
func successScore(w snap.Window) *model.SuccessScore { return overallScore(w.SuccessOverall) }

// challengeScore is a window's overall challenge score (CHALLENGE-OVERALL-1);
// nil (null) when the snapshot has none.
func challengeScore(w snap.Window) *model.SuccessScore { return overallScore(w.ChallengeOverall) }

// overallScore is a snapshot overall as served; nil (null) without one.
func overallScore(o *snap.SuccessOverall) *model.SuccessScore {
	if o == nil {
		return nil
	}
	s := &model.SuccessScore{MetricScore: model.MetricScore{Score: o.Score, Rank: o.Rank, RankedOf: o.RankedOf, Status: o.Status},
		Targets: o.Targets, Weights: o.Weights}
	if s.Targets == nil {
		s.Targets = map[string]Num{}
	}
	if s.Weights == nil {
		s.Weights = map[string]Num{}
	}
	return s
}

// rowSuccessScore is successScore without its targets and weights.
func rowSuccessScore(w snap.Window) *model.MetricScore {
	if s := successScore(w); s != nil {
		return &s.MetricScore
	}
	return nil
}

// scoreDelta is score minus previous, the previous published build's score
// (SUCCESS-SCORE-DELTA-1): unrounded, null (NaN) when either is null.
func scoreDelta(score, previous Num) Num {
	if !score.OK() || !previous.OK() {
		return nan
	}
	return score - previous
}

// successScoreDelta is a window's overall success score minus the previous
// build's; null when the snapshot has no overall score or no previous one.
func successScoreDelta(w snap.Window) Num {
	if o := w.SuccessOverall; o != nil {
		return scoreDelta(o.Score, o.PreviousScore)
	}
	return nan
}

// NANoScoreToCompare is the reason code of a null successScoreDelta or
// scores.success.delta (answer_na.go): this build or the previous published
// one has no ranked success score for the network, range (and target).
const NANoScoreToCompare = "no_score_to_compare"

// isScoreDelta reports whether a path pattern is a success score delta's.
func isScoreDelta(p string) bool {
	return strings.HasPrefix(figure(p), "successScoreDelta") || strings.HasSuffix("."+p, ".success.delta")
}

// ratedRate is one network's success rate and its requests.
type ratedRate struct {
	rate, requests Num
}

// metricScores is each rated network's score, in order: metric_scores of
// calculated/target_scores.py, higher rates first.
func metricScores(rated []ratedRate) []model.MetricScore {
	out := make([]model.MetricScore, len(rated))
	ranked := []int{}
	for i, r := range rated {
		out[i] = model.MetricScore{Score: nan, Rank: nan, RankedOf: nan, Status: notEnoughData}
		if r.rate.OK() && r.requests.OK() && r.requests >= scoreMinRequests {
			ranked = append(ranked, i)
		}
	}
	status := scoreRanked
	if len(ranked) < scoreMinRanked {
		status = scoreTooFewNetworks
	}
	rates := make([]float64, len(ranked))
	for k, i := range ranked {
		rates[k] = rated[i].rate.F()
	}
	ranks := averageRanks(rates)
	// A tight race is a shared first place, a 10 for every ranked network
	// (SUCCESS-TIE-1).
	if status == scoreRanked && tightRace(rated, ranked) {
		for k := range ranks {
			ranks[k] = 1
		}
	}
	m, best := float64(len(ranked)), 1.0
	if len(ranks) > 0 {
		best = slices.Min(ranks)
	}
	for k, i := range ranked {
		out[i].Status = status
		if status == scoreRanked {
			out[i].Score, out[i].Rank, out[i].RankedOf = shiftedRankScore(ranks[k], best, m), Num(ranks[k]), Num(m)
		}
	}
	return out
}

// rankScore is the 1-10 score of a rank among m ranked networks.
func rankScore(rank, m float64) Num { return Num(1 + 9*(m-rank)/(m-1)) }

// tightRace is similar() of calculated/target_scores.py over the ranked
// networks: the best and worst rates (of each, the one with the most
// requests) are closer than the larger of scoreTightPts points and
// scoreTightZ standard errors of their difference.
func tightRace(rated []ratedRate, ranked []int) bool {
	best, worst := rated[ranked[0]], rated[ranked[0]]
	for _, i := range ranked[1:] {
		r := rated[i]
		if r.rate > best.rate || r.rate == best.rate && r.requests > best.requests {
			best = r
		}
		if r.rate < worst.rate || r.rate == worst.rate && r.requests > worst.requests {
			worst = r
		}
	}
	p1, n1, p2, n2 := best.rate.F(), best.requests.F(), worst.rate.F(), worst.requests.F()
	se := math.Sqrt(p1*(1-p1)/n1 + p2*(1-p2)/n2)
	return p1-p2 < math.Max(scoreTightPts/100, scoreTightZ*se)
}

// averageRanks is each value's rank, the highest 1, ties sharing the average
// of their ranks.
func averageRanks(values []float64) []float64 {
	order := make([]int, len(values))
	for i := range order {
		order[i] = i
	}
	sort.SliceStable(order, func(a, b int) bool { return values[order[a]] > values[order[b]] })
	ranks := make([]float64, len(values))
	for start := 0; start < len(order); {
		end := start
		for end+1 < len(order) && values[order[end+1]] == values[order[start]] {
			end++
		}
		for _, i := range order[start : end+1] {
			ranks[i] = float64(start+end)/2 + 1
		}
		start = end + 1
	}
	return ranks
}

// placeScores sets each place row's successScore among the rows of its kind:
// its success rate ranked with metricScores, MIN_REQUESTS being requests at
// the place.
func placeScores(rows []PlaceRow) {
	byKind := map[string][]int{}
	for i, r := range rows {
		byKind[r.Kind] = append(byKind[r.Kind], i)
	}
	for _, idx := range byKind {
		rated := make([]ratedRate, len(idx))
		for k, i := range idx {
			rated[k] = ratedRate{rows[i].SuccessRate, rows[i].Requests}
		}
		for k, s := range metricScores(rated) {
			rows[idx[k]].SuccessScore = &s
		}
	}
}

// MarketScoreBasis names what the market score is computed from.
const MarketScoreBasis = "median_success_rate"

// MarketScore is the home market's success score: 1 + 9 × the median
// network's success rate, to 1 decimal; Score and Status are null without
// that median.
type MarketScore struct {
	Score  Num       `json:"score"`
	Status model.Str `json:"status"`
	Basis  string    `json:"basis"`
}

func marketScore(median Num) MarketScore { return absoluteScore(median, MarketScoreBasis) }

// Bases of the other absolute scores (SUCCESS-SCORE-ALL-1): a target's fleet
// mean and the events' pass rate.
const (
	FleetScoreBasis = "mean_success_rate"
	PassScoreBasis  = "success_rate"
)

// absoluteScore is a rate on the 1-10 scale, as the market's: 1 + 9 × rate,
// to 1 decimal; Score and Status are null without the rate. A median or mean
// of rank scores is always about 5.5, so the market, a target's fleet and the
// events' pass are scored on their rate, not ranked.
func absoluteScore(rate Num, basis string) MarketScore {
	return MarketScore{Score: absoluteValue(rate), Status: rankedIf(rate), Basis: basis}
}

// absoluteValue is 1 + 9 × rate to 1 decimal, NaN without the rate.
func absoluteValue(rate Num) Num {
	if !rate.OK() {
		return nan
	}
	return Num(math.Round((1+9*rate.F())*10) / 10)
}

// rankedIf is "ranked" when v is set, else null.
func rankedIf(v Num) model.Str {
	if v.OK() {
		return model.S(scoreRanked)
	}
	return nil
}

// ScoreSeries is a series of 1-10 scores: each point's score (null unless
// ranked) and, in Statuses, why. A ranked spark's point without a rate is
// not_enough_data; an absolute spark's (the market's) has a null status.
type ScoreSeries struct {
	Values   []Num       `json:"values"`
	Statuses []model.Str `json:"statuses"`
}

// ratedPoint is one point of a network's success history: when it is, its
// rate and its requests.
type ratedPoint struct {
	at   int64
	rate ratedRate
}

// pointScores is, per network (by key), the score of each of its points
// (in its order): each point ranked with metricScores among the networks
// with a point at the same time (MIN_REQUESTS being the point's requests;
// fewer than scoreMinRanked such networks give too_few_networks).
func pointScores(series map[string][]ratedPoint) map[string][]model.MetricScore {
	type at struct {
		key string
		i   int
	}
	byTime := map[int64][]at{}
	out := map[string][]model.MetricScore{}
	for key, points := range series {
		out[key] = make([]model.MetricScore, len(points))
		for i, p := range points {
			byTime[p.at] = append(byTime[p.at], at{key, i})
		}
	}
	for _, members := range byTime {
		sort.Slice(members, func(a, b int) bool { return members[a].key < members[b].key })
		rated := make([]ratedRate, len(members))
		for k, m := range members {
			rated[k] = series[m.key][m.i].rate
		}
		for k, s := range metricScores(rated) {
			out[members[k].key][members[k].i] = s
		}
	}
	return out
}
