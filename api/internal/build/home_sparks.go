package build

import (
	"time"

	"github.com/Zeus-Development/atlas/api/internal/model"
)

// The home's sparks on the curve axis (CURVE-HOURLY-DAILY-1, #1301): one
// point per UTC hour (24h) or day (7d, 30d) over the whole range, as the score
// curves, so every network series shares one times list.
//
//   - A network's latency is its series row of the hour or day: the calculator
//     times it over the hour's or day's rows (merged histograms,
//     SERIES-HOURLY-DAILY-1). Percentiles are never averaged: a bucket with
//     several rows (a snapshot from before it, 5-minute rows) is null.
//   - The market's latency is the median of the fleet networks' latency of the
//     hour or day, as market.medianLatency is of their range latency; null
//     below marketLatencyMinNetworks networks with one.
//   - A network's success score of an hour or day is its requests-weighted
//     rate ranked among the home's networks of its kind with requests then
//     (pointScores); the market's is its curve's rate, absolute.

// marketLatencyMinNetworks is how many fleet networks need a latency in an
// hour or day for the market's latency there (too_few_networks's minimum).
const marketLatencyMinNetworks = scoreMinRanked

// onePerBucket is the value of each bucket's only point; NaN in a bucket
// without one or with several.
func (ax curveAxis) onePerBucket(points []curvePoint) []Num {
	out, count := make([]Num, ax.n), make([]int, ax.n)
	for _, p := range points {
		if i := ax.index(p.mid); i >= 0 {
			count[i], out[i] = count[i]+1, Num(p.rate)
		}
	}
	for i, c := range count {
		if c != 1 {
			out[i] = nan
		}
	}
	return out
}

// entryLatencyPoints is a provider's latency (p50) history for the range as
// curve points (rate the latency), as entryCurvePoints is its success: its
// headline snapshot product's series rows, else its window stats series.
func entryLatencyPoints(e *entry, s *WindowStats, rangeID string, asOf time.Time) []curvePoint {
	out := []curvePoint{}
	product := e.Snap[e.P.PrimaryKind]
	if product == nil {
		for _, p := range s.Latency {
			if p.T.OK() && p.P50.OK() {
				out = append(out, curvePoint{mid: asOf.Add(-time.Duration(p.T.F() * float64(time.Minute))), rate: p.P50.F(), weight: 1})
			}
		}
		return out
	}
	for _, p := range windowHistory(product, rangeID, true) {
		out = append(out, curvePoint{mid: p.at.Add(p.span / 2), rate: p.value.F(), weight: 1})
	}
	return out
}

// latencySpark is a network's latency on the axis; empty without history.
func latencySpark(points []curvePoint, ax curveAxis) SparkSeries {
	if len(points) == 0 {
		return SparkSeries{Values: []Num{}, Times: []Num{}}
	}
	return SparkSeries{Values: ax.onePerBucket(points), Times: ax.times()}
}

// marketLatency is, per bucket, the median of the networks' latency there
// (lists: each network's onePerBucket), NaN below marketLatencyMinNetworks.
func marketLatency(lists [][]Num, ax curveAxis) SparkSeries {
	s := SparkSeries{Values: make([]Num, ax.n), Times: ax.times()}
	for i := range s.Values {
		values := []float64{}
		for _, l := range lists {
			if l[i].OK() {
				values = append(values, l[i].F())
			}
		}
		s.Values[i] = nan
		if len(values) >= marketLatencyMinNetworks {
			s.Values[i] = Num(median(values))
		}
	}
	return s
}

// fleetLatency is the market's latency spark from the fleet's networks.
func fleetLatency(fleet []*entry, stats map[string]map[string]*WindowStats, ax curveAxis, rangeID string) SparkSeries {
	lists := [][]Num{}
	for _, e := range fleet {
		if s := stats[e.P.Slug][rangeID]; s != nil {
			lists = append(lists, ax.onePerBucket(entryLatencyPoints(e, s, rangeID, ax.asOf)))
		}
	}
	return marketLatency(lists, ax)
}

// axisPointScores is, by slug, each row's success score per bucket of the
// axis: its bucket rates (hists) ranked with pointScores among the rows of
// its kind with requests in that bucket; not_enough_data elsewhere, and in
// every bucket of a row without a snapshot product (no request counts). A
// row without history has none.
func axisPointScores(rows []RankingRow, bySlug map[string]*entry, hists []homeHistory, ax curveAxis) map[string][]model.MetricScore {
	byKind, out := map[string]map[string][]ratedPoint{}, map[string][]model.MetricScore{}
	for j, row := range rows {
		if len(hists[j].curve) == 0 {
			continue
		}
		out[row.Slug] = unrated(ax.n)
		if e := bySlug[row.Slug]; e == nil || e.Snap[e.P.PrimaryKind] == nil {
			continue
		}
		points := []ratedPoint{}
		for i, r := range ax.rated(hists[j].curve) {
			if r.requests > 0 {
				points = append(points, ratedPoint{at: int64(i), rate: r})
			}
		}
		if byKind[row.Kind] == nil {
			byKind[row.Kind] = map[string][]ratedPoint{}
		}
		byKind[row.Kind][row.Slug] = points
	}
	for _, series := range byKind {
		for slug, scores := range pointScores(series) {
			for k, p := range series[slug] {
				out[slug][p.at] = scores[k]
			}
		}
	}
	return out
}

// marketSeries sets the market's curve and sparks on the axis: the success
// curve and its absolute score spark (1 decimal) from the fleet's pooled
// rates, the latency spark the median of the networks' latency.
func (a *HomeAnswer) marketSeries(fleet []*entry, stats map[string]map[string]*WindowStats, ax curveAxis, rangeID string) {
	rates, m := fleetCurve(fleet, stats, ax, rangeID), &a.Market
	m.SuccessScoreCurve, m.SuccessScoreSpark = scoreCurve(rates), marketScoreSpark(rates)
	m.SuccessSpark = roundedSeries(rates, homeRateDecimals)
	m.LatencySpark = roundedSeries(fleetLatency(fleet, stats, ax, rangeID), homeLatencyDecimals)
}

// scoreSeries is scores as a ScoreSeries, values to homeScoreDecimals.
func scoreSeries(scores []model.MetricScore) ScoreSeries {
	s := ScoreSeries{Values: make([]Num, len(scores)), Statuses: make([]model.Str, len(scores))}
	for i, m := range scores {
		s.Values[i], s.Statuses[i] = m.Score, model.S(m.Status)
	}
	s.Values = roundedValues(s.Values, homeScoreDecimals)
	return s
}
