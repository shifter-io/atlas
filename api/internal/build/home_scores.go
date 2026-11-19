package build

import (
	"github.com/Zeus-Development/atlas/api/internal/jsnum"
	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/snap"
)

// The home's success score series (SUCCESS-SCORE-ALL-1): the market's spark
// and delta are absolute (absoluteScore), as market.successScore; each
// network's spark is ranked per point among the home's networks of its kind.

// HomeScoreSeries is a network's success score spark: its points are the
// success spark's, and so is its TimesIndex (its index into HomeAnswer.Times).
type HomeScoreSeries struct {
	ScoreSeries
	TimesIndex int `json:"timesIndex"`
}

// MarketScoreSeries is the market's success score spark, with the success
// spark's times.
type MarketScoreSeries struct {
	ScoreSeries
	Times []Num `json:"times"`
}

// marketScoreSpark is the market's success spark on the 1-10 scale: 1 + 9 ×
// each point's rate, with its times.
func marketScoreSpark(spark SparkSeries) MarketScoreSeries {
	s := MarketScoreSeries{ScoreSeries: ScoreSeries{Values: make([]Num, len(spark.Values)), Statuses: make([]model.Str, len(spark.Values))},
		Times: spark.Times}
	for i, v := range spark.Values {
		s.Values[i], s.Statuses[i] = absoluteValue(v), rankedIf(v)
	}
	return s
}

// scoreCurve is a success history s on the 1-10 scale, as absoluteScore
// (1 + 9 × each point's rate) but to homeScoreDecimals decimals, as it is
// drawn: a gap (null) stays null, and so do the times.
func scoreCurve(s SparkSeries) SparkSeries {
	out := SparkSeries{Values: make([]Num, len(s.Values)), Times: s.Times}
	for i, v := range s.Values {
		out.Values[i] = nan
		if v.OK() {
			out.Values[i] = Num(jsnum.FixedNum(1+9*v.F(), homeScoreDecimals))
		}
	}
	return out
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
// with no headline snapshot product) scores every point not_enough_data; a
// held window (STANDING-FREEZE-1) keeps its stored point scores and is no
// peer.
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
		history := measuredHistory(product, rangeID, false)
		if product.Window(rangeID).Held() {
			stored := make([]*snap.PointScore, len(history))
			for i, p := range history {
				stored[i] = p.score
			}
			out[row.Slug] = storedPointScores(stored)
			continue
		}
		points := []ratedPoint{}
		for _, p := range history {
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

// thinScores is scores downsampled to count points as a ScoreSeries.
func thinScores(scores []model.MetricScore, count int) ScoreSeries {
	thinned := downsample(scores, count)
	s := ScoreSeries{Values: make([]Num, len(thinned)), Statuses: make([]model.Str, len(thinned))}
	for i, m := range thinned {
		s.Values[i], s.Statuses[i] = m.Score, model.S(m.Status)
	}
	return s
}

// HomeReliable is the market's most reliable network: its success rate
// (Value, kept until #900 removes the rates) and its success score with its
// status.
type HomeReliable struct {
	Slug   string    `json:"slug"`
	Name   string    `json:"name"`
	Value  Num       `json:"-"` // internal (SUCCESS-RATE-REMOVE-1, #900): its success rate; Score is served
	Score  Num       `json:"score"`
	Status model.Str `json:"status"`
}

// mostReliable is the fleet network with the highest success score in the
// range, a tie (also across kinds) to the higher success rate, then to the
// first; without any score, byRate (the highest success rate) with its null
// score. Nil without either.
func mostReliable(fleet []*entry, stats map[string]map[string]*WindowStats, rangeID string, byRate StatPick) *HomeReliable {
	var best *entry
	var top *WindowStats
	for _, e := range fleet {
		s := stats[e.P.Slug][rangeID]
		if s != nil && s.SuccessScore != nil && s.SuccessScore.Score.OK() && (top == nil || moreReliable(s, top)) {
			best, top = e, s
		}
	}
	switch {
	case top != nil:
		return reliable(best.P.Slug, best.P.Name, top)
	case byRate.Stats != nil:
		return reliable(byRate.Provider.Slug, byRate.Provider.Name, byRate.Stats)
	}
	return nil
}

// moreReliable is whether a's success score beats b's, a tie going to the
// higher success rate.
func moreReliable(a, b *WindowStats) bool {
	if a.SuccessScore.Score != b.SuccessScore.Score {
		return a.SuccessScore.Score > b.SuccessScore.Score
	}
	return a.SuccessRate.OK() && (!b.SuccessRate.OK() || a.SuccessRate > b.SuccessRate)
}

func reliable(slug, name string, s *WindowStats) *HomeReliable {
	r := &HomeReliable{Slug: slug, Name: name, Value: s.SuccessRate, Score: nan}
	if sc := s.SuccessScore; sc != nil {
		r.Score, r.Status = sc.Score, model.S(sc.Status)
	}
	return r
}

// roundedSeries is s with its values rounded to dp decimals (Number
// toFixed), null points kept; s itself is unchanged.
func roundedSeries(s SparkSeries, dp int) SparkSeries {
	return SparkSeries{Values: roundedValues(s.Values, dp), Times: s.Times}
}

// roundedValues is a copy of values rounded to dp decimals; NaN (null) stays.
func roundedValues(values []Num, dp int) []Num {
	out := make([]Num, len(values))
	for i, v := range values {
		out[i] = Num(jsnum.FixedNum(v.F(), dp))
	}
	return out
}
