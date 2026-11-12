package build

import (
	"github.com/Zeus-Development/proxy-index/api/internal/jsnum"
	"github.com/Zeus-Development/proxy-index/api/internal/model"
)

// The home's success score series (SUCCESS-SCORE-ALL-1): the market's spark
// and delta are absolute (absoluteScore), as market.successScore; each
// network's spark is ranked per point among the home's networks of its kind.

// HomeScoreSeries is a network's success score spark: its points are the
// success spark's, and so are its labels (their index in HomeAnswer.Labels).
type HomeScoreSeries struct {
	ScoreSeries
	Labels int `json:"labels"`
}

// marketScoreSpark is the market's success spark on the 1-10 scale: 1 + 9 ×
// each point's rate; its labels are the success spark's.
func marketScoreSpark(spark SparkSeries) ScoreSeries {
	s := ScoreSeries{Values: make([]Num, len(spark.Values)), Status: make([]model.Str, len(spark.Values))}
	for i, v := range spark.Values {
		s.Values[i], s.Status[i] = absoluteValue(v), rankedIf(v)
	}
	return s
}

// marketScoreDelta is the market success history's change on the 1-10
// scale: 9 × the closing fifth's mean rate minus the opening fifth's (as a
// network's successDeltaPts), to 2 decimals; NaN below two observed points.
func marketScoreDelta(history []model.SeriesPoint) Num {
	observed := []model.SeriesPoint{}
	for _, p := range history {
		if p.V.OK() {
			observed = append(observed, p)
		}
	}
	if len(observed) < 2 {
		return nan
	}
	return Num(jsnum.FixedNum(9*trendPts(observed)/100, 2))
}

// homePointScores is, by slug, the score of each point of a ranking row's
// success history (as HomeAnswer.network reads it): ranked with pointScores
// among the rows of its kind. A history without request counts (a network
// with no headline snapshot product) scores every point not_enough_data.
func homePointScores(rows []RankingRow, bySlug map[string]*entry, stats map[string]map[string]*WindowStats, rangeID string) map[string][]model.MetricScore {
	byKind, out := map[string]map[string][]ratedPoint{}, map[string][]model.MetricScore{}
	for _, row := range rows {
		e, s := bySlug[row.Slug], stats[row.Slug][rangeID]
		if e == nil || s == nil {
			continue
		}
		product := e.Snap[e.P.PrimaryKind]
		if product == nil {
			out[row.Slug] = unrated(len(s.Success))
			continue
		}
		points := []ratedPoint{}
		for _, p := range measuredHistory(product, rangeID, false) {
			points = append(points, ratedPoint{at: p.at.UnixNano(), rate: ratedRate{p.value, p.requests}})
		}
		if byKind[row.Kind] == nil {
			byKind[row.Kind] = map[string][]ratedPoint{}
		}
		byKind[row.Kind][row.Slug] = points
	}
	for _, series := range byKind {
		for slug, scores := range pointScores(series) {
			out[slug] = scores
		}
	}
	return out
}

// unrated is n points without a score: not_enough_data.
func unrated(n int) []model.MetricScore {
	out := make([]model.MetricScore, n)
	for i := range out {
		out[i] = model.MetricScore{Score: nan, Rank: nan, RankedOf: nan, Status: notEnoughData}
	}
	return out
}

// scoreSpark thins a network's point scores as its success spark is
// thinned (downsample to count points), with that spark's labels.
func scoreSpark(scores []model.MetricScore, count, labels int) HomeScoreSeries {
	thinned := downsample(scores, count)
	s := HomeScoreSeries{ScoreSeries: ScoreSeries{Values: make([]Num, len(thinned)), Status: make([]model.Str, len(thinned))}, Labels: labels}
	for i, m := range thinned {
		s.Values[i], s.Status[i] = m.Score, model.S(m.Status)
	}
	return s
}
