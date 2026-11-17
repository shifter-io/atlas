package build

import (
	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/snap"
)

// rankingPointScores is, by slug, the success score of each series row of
// a ranking source's window of the range (its trend's and chart's points):
// the calculator's point score (SUCCESS-POINTS-1, #1301), the window's
// Success x/10 rule over the daily tests of the trailing window ending at that hour or day, so a steady
// network's points read its headline. A source without a snapshot group, or
// a row without a score (a snapshot from before it), is not_enough_data.
func rankingPointScores(srcs []rankingSource, rangeID string) map[string][]model.MetricScore {
	out := map[string][]model.MetricScore{}
	for _, src := range srcs {
		s := src.stats[rangeID]
		switch {
		case s == nil:
			continue
		case src.sp == nil:
			out[src.p.Slug] = unrated(len(s.Success))
			continue
		}
		rows := src.sp.Window(rangeID).Series
		scores := make([]model.MetricScore, len(rows))
		for i, row := range rows {
			scores[i] = seriesScore(row)
		}
		out[src.p.Slug] = scores
	}
	return out
}

// seriesScore is a series row's success score as a MetricScore (no rank):
// its score when ranked, else null with its status; not_enough_data
// without one.
func seriesScore(row snap.SeriesRow) model.MetricScore {
	m := model.MetricScore{Score: nan, Rank: nan, RankedOf: nan, Status: notEnoughData}
	if p := row.SuccessScore; p != nil && p.Status != "" {
		m.Status = p.Status
		if p.Status == scoreRanked {
			m.Score = p.Score
		}
	}
	return m
}
