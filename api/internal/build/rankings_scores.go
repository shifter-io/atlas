package build

import (
	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/snap"
)

// rankingPointScores is, by slug, the score of each point of a ranking row's
// success history (its WindowStats.Success, one point per window series row,
// which its trend thins): ranked with pointScores among the rows of its
// kind, each point's requests being its series row's. A history without
// request counts (no snapshot group) scores every point not_enough_data; a
// held window (STANDING-FREEZE-1) keeps its stored point scores and is no
// peer.
func rankingPointScores(srcs []rankingSource, rangeID string) map[string][]model.MetricScore {
	byKind, out := map[string]map[string][]ratedPoint{}, map[string][]model.MetricScore{}
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
		points := []ratedPoint{}
		for _, row := range src.sp.Window(rangeID).Series {
			points = append(points, ratedPoint{at: int64(snap.Ms(row.At)), rate: ratedRate{row.SuccessRate, row.Completed}})
		}
		if byKind[src.kind] == nil {
			byKind[src.kind] = map[string][]ratedPoint{}
		}
		byKind[src.kind][src.p.Slug] = points
	}
	for _, series := range byKind {
		for slug, scores := range pointScores(series) {
			out[slug] = scores
		}
	}
	return out
}
