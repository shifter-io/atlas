package build

import (
	"time"

	"github.com/shifter-io/atlas/api/internal/model"
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
//   - A network's success score of an hour or day is the calculator's point
//     score (SUCCESS-POINTS-1): the window's Success x/10 rule over the daily
//     tests of the trailing window of the range's length ending then
//     (SUCCESS-TRAILING-1); the market's is 1 + 9 × the median of the
//     networks' trailing success rates, as market.successScore is of their
//     window rates. Each newest point is its headline.

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

// scorePoint is a series row's success score at the middle of its time.
type scorePoint struct {
	mid   time.Time
	score model.MetricScore
}

// entryScorePoints is a provider's range window's series rows' success
// scores (seriesScore); none without a snapshot product.
func entryScorePoints(e *entry, rangeID string) []scorePoint {
	product := e.Snap[e.P.PrimaryKind]
	if product == nil {
		return nil
	}
	w := product.Windows[rangeID]
	out := []scorePoint{}
	for _, row := range w.Series {
		if at, err := time.Parse(time.RFC3339Nano, row.At); err == nil {
			out = append(out, scorePoint{mid: at.Add(seriesStep(rangeID) / 2), score: seriesScore(row)})
		}
	}
	return out
}

// axisScores is a network's success score per bucket of the axis: its one
// row's there (the calculator's, SUCCESS-POINTS-1: the window's Success x/10
// rule over the daily tests of the trailing window ending at the hour or day); not_enough_data in a bucket
// without one or with several, and in every bucket of a network without
// request counts. A network without history has none.
func axisScores(h homeHistory, ax curveAxis) []model.MetricScore {
	if len(h.curve) == 0 {
		return nil
	}
	out, count := unrated(ax.n), make([]int, ax.n)
	for _, p := range h.scores {
		if i := ax.index(p.mid); i >= 0 {
			count[i], out[i] = count[i]+1, p.score
		}
	}
	for i, c := range count {
		if c > 1 {
			out[i] = unrated(1)[0]
		}
	}
	return out
}

// fleetMedianRates is, per bucket, the median of the fleet networks'
// trailing success rates there (each network's series row's: its window's
// success rate over the trailing window of the window's length ending at the
// row, SUCCESS-TRAILING-1), as market.successScore is 1 + 9 × the median of
// their window rates, so the newest point is the headline's; NaN where no
// network has one (and in a snapshot from before it).
func fleetMedianRates(fleet []*entry, ax curveAxis, rangeID string) SparkSeries {
	lists := [][]Num{}
	for _, e := range fleet {
		lists = append(lists, ax.onePerBucket(entryTrailingRates(e, rangeID)))
	}
	return medianRates(lists, ax)
}

// entryTrailingRates is a provider's range window's series rows' trailing
// success rates at their middles (rate the trailing rate); none without a
// snapshot product.
func entryTrailingRates(e *entry, rangeID string) []curvePoint {
	product := e.Snap[e.P.PrimaryKind]
	if product == nil {
		return nil
	}
	out := []curvePoint{}
	for _, row := range product.Windows[rangeID].Series {
		r := row.TrailingSuccessRate
		if at, err := time.Parse(time.RFC3339Nano, row.At); err == nil && r != nil && r.OK() {
			out = append(out, curvePoint{mid: at.Add(seriesStep(rangeID) / 2), rate: r.F(), weight: 1})
		}
	}
	return out
}

// medianRates is, per bucket, the median of lists' values there (each list
// one network's per bucket); NaN where none has one.
func medianRates(lists [][]Num, ax curveAxis) SparkSeries {
	out := SparkSeries{Values: make([]Num, ax.n), Times: ax.times()}
	for i := range out.Values {
		rates := []float64{}
		for _, l := range lists {
			if l[i].OK() {
				rates = append(rates, l[i].F())
			}
		}
		out.Values[i] = Num(median(rates))
	}
	return out
}

// marketSeries sets the market's curve and sparks on the axis: the success
// curve from the fleet's pooled rates, the score spark (1 decimal) 1 + 9 ×
// the median of the networks' trailing rates (market.successScore's rule over
// the trailing window ending at each hour or day), the latency spark the
// median of the networks' latency.
func (a *HomeAnswer) marketSeries(fleet []*entry, stats map[string]map[string]*WindowStats, ax curveAxis, rangeID string) {
	rates, m := fleetCurve(fleet, stats, ax, rangeID), &a.Market
	m.SuccessScoreCurve = scoreCurve(rates)
	m.SuccessScoreSpark = marketScoreSpark(fleetMedianRates(fleet, ax, rangeID))
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
