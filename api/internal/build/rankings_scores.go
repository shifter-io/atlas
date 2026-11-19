package build

import (
	"github.com/shifter-io/atlas/api/internal/model"
	"github.com/shifter-io/atlas/api/internal/snap"
)

// rankingPointScores is, by slug, the score of each point of a ranking row's
// success history (its WindowStats.Success, one point per window series row,
// which its trend thins): ranked with pointScores among the rows of its
// kind, each point's requests being its series row's. A history without
// request counts (no snapshot group) scores every point not_enough_data; a
// held window (STANDING-FREEZE-1) keeps its stored point scores and is no
// peer.
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
		case src.sp.Window(rangeID).Held():
			out[src.p.Slug] = storedPointScores(seriesPointScores(src.sp.Window(rangeID)))
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
// its score when ranked (or partial_targets, a success overall without a card
// target), else null with its status; not_enough_data without one.
func seriesScore(row snap.SeriesRow) model.MetricScore {
	m := model.MetricScore{Score: nan, Rank: nan, RankedOf: nan, Status: notEnoughData}
	if p := row.SuccessScore; p != nil && p.Status != "" {
		m.Status = p.Status
		if p.Status == scoreRanked || p.Status == scorePartialTargets {
			m.Score = p.Score
		}
	}
	return m
}
