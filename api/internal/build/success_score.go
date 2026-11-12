package build

import (
	"math"
	"sort"
	"strings"

	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/Zeus-Development/proxy-index/api/internal/snap"
)

// Success scores (SUCCESS-SCORE-API-1): the 1-10 scores the website shows in
// place of success rates. A target's and a network's overall score are the
// calculator's (proxy-index calculated/target_scores.py); a place row's is
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
	scoreAllSimilar      = "all_similar"
	scoreNoRankedTargets = "no_ranked_targets"
)

// successScore is a window's overall success score; nil (null) when the
// snapshot has none.
func successScore(w snap.Window) *model.SuccessScore {
	o := w.SuccessOverall
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

// noScoreToCompare is the /v1/coverage reason of those nulls (na.go).
const noScoreToCompare = "No ranked success score in this build or the previous published build for this network and range (and target)"

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
	switch {
	case len(ranked) < scoreMinRanked:
		status = scoreTooFewNetworks
	case tightRace(rated, ranked):
		status = scoreAllSimilar
	}
	rates := make([]float64, len(ranked))
	for k, i := range ranked {
		rates[k] = rated[i].rate.F()
	}
	ranks := averageRanks(rates)
	m := float64(len(ranked))
	for k, i := range ranked {
		out[i].Status = status
		if status == scoreRanked {
			out[i].Score, out[i].Rank, out[i].RankedOf = Num(1+9*(m-ranks[k])/(m-1)), Num(ranks[k]), Num(m)
		}
	}
	return out
}

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

func marketScore(median Num) MarketScore {
	m := MarketScore{Score: nan, Basis: MarketScoreBasis}
	if median.OK() {
		m.Score, m.Status = Num(math.Round((1+9*median.F())*10)/10), model.S(scoreRanked)
	}
	return m
}

// ScoreDefinitions are the plain-word tooltips of the 1-10 scores.
type ScoreDefinitions struct {
	Rank   string            `json:"rank"`
	Market string            `json:"market"`
	Status map[string]string `json:"status"`
}

// scoreDefinitions is every answer's ScoreDefinitions.
var scoreDefinitions = ScoreDefinitions{
	Rank:   "1 to 10: where the network ranks among the tested networks on this measure. 10 is the best, 1 the lowest.",
	Market: "1 to 10: the market's typical success rate on a 10-point scale. 10 means every request succeeded.",
	Status: map[string]string{
		scoreRanked:          "Scored: enough networks had enough tests here to compare them.",
		scoreAllSimilar:      "No score: the networks were too close to tell apart here.",
		notEnoughData:        "No score: this network had fewer than 100 tests here.",
		scoreTooFewNetworks:  "No score: fewer than 3 networks had enough tests here to compare.",
		scoreNoRankedTargets: "No score: none of this network's targets has a score yet.",
	},
}
