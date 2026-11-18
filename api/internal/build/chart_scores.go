package build

import (
	"github.com/shifter-io/atlas/api/internal/jsnum"
	"github.com/shifter-io/atlas/api/internal/model"
)

// ChartPoint is a chart answer's success point: its time and, on a
// network's chart, the point's 1-10 success score (CHART-SCORE-DCIPS-1). Its
// rate is internal (SUCCESS-RATE-REMOVE-1, #900): the score reads it.
type ChartPoint struct {
	T Num `json:"t"`
	V Num `json:"-"`
	*PointScore
}

// PointScore is a chart point's Success x/10, the calculator's point score
// (the window's rule over the point's hour or day, SUCCESS-POINTS-1), as the
// rankings' successScoreSpark (rankingPointScores),
// to homeScoreDecimals; Score is null unless Status is ranked.
type PointScore struct {
	Score  Num       `json:"score"`
	Status model.Str `json:"status"`
}

// chartPoints is a success history as chart points without scores.
func chartPoints(points []model.SeriesPoint) []ChartPoint {
	out := make([]ChartPoint, len(points))
	for i, p := range points {
		out[i] = ChartPoint{T: p.T, V: p.V}
	}
	return out
}

// scoreCharts gives every point of each network chart's success history its
// point score (rankingPointScores over rankingSources of the chart's kind,
// whose histories are the charts'). A chart whose history and scores differ
// in length (none should) scores every point not_enough_data.
func (pg *Pages) scoreCharts(entries []*entry, stats map[string]map[string]*WindowStats, details map[string]*ProviderDetail) {
	for _, kind := range model.ProxyKinds {
		srcs := rankingSources(entries, stats, details, kind)
		for _, r := range Ranges {
			scores := rankingPointScores(srcs, r.ID)
			for _, src := range srcs {
				if c := pg.Charts[src.p.Slug+"/"+kind+"/"+r.ID]; c != nil {
					scorePoints(c.Success, scores[src.p.Slug])
				}
			}
		}
	}
}

// scorePoints sets each point's score from scores (in the points' order).
func scorePoints(points []ChartPoint, scores []model.MetricScore) {
	if len(scores) != len(points) {
		scores = unrated(len(points))
	}
	for i, s := range scores {
		points[i].PointScore = &PointScore{Score: Num(jsnum.FixedNum(s.Score.F(), homeScoreDecimals)), Status: model.S(s.Status)}
	}
}
