package build

import (
	"slices"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/model"
)

func pointsAt(v Num, times ...Num) []model.SeriesPoint {
	out := []model.SeriesPoint{}
	for _, t := range times {
		out = append(out, model.SeriesPoint{T: t, V: v})
	}
	return out
}

// The grid is every time of the rows' histories once, oldest first; a
// one-day range thins it to the 24h curve count.
func TestCurveGridIsEveryHistoryTimeOldestFirst(t *testing.T) {
	t.Parallel()
	hists := []homeHistory{{success: pointsAt(0.9, 120, 60, 0)}, {}, {success: pointsAt(0.8, 180, 60, nan)}}
	if g := curveGrid(hists, rangeMeta("7d")); !slices.Equal(g, []Num{180, 120, 60, 0}) {
		t.Errorf("7d grid %v", g)
	}
	day := []model.SeriesPoint{}
	for m := 1435; m >= 0; m -= 5 {
		day = append(day, model.SeriesPoint{T: Num(m), V: 1})
	}
	g := curveGrid([]homeHistory{{success: day}, {success: day[1:]}}, rangeMeta("24h"))
	if len(g) != homeCurve24h || g[0] != 1435 || g[len(g)-1] != 0 {
		t.Errorf("24h grid of %d points, %v ... %v", len(g), g[0], g[len(g)-1])
	}
	if g := curveGrid([]homeHistory{{}}, rangeMeta("30d")); len(g) != 0 {
		t.Errorf("grid without history %v", g)
	}
}

// A curve on the grid has a value per grid time, null at the network's gaps
// and at times between its points; a row without history has none.
func TestGridSeriesHasNullAtTheGaps(t *testing.T) {
	t.Parallel()
	grid := []Num{180, 120, 60, 0}
	s := gridSeries([]model.SeriesPoint{{T: 120, V: 0.5}, {T: 0, V: 0.75}, {T: 30, V: 1}}, grid)
	if len(s.Values) != 4 || s.Values[0].OK() || s.Values[1] != 0.5 || s.Values[2].OK() || s.Values[3] != 0.75 {
		t.Errorf("values %v", s.Values)
	}
	checkTimes(t, "grid series", len(s.Values), s.Labels, s.Times)
	if e := gridSeries(nil, grid); e.Values == nil || len(e.Values)+len(e.Labels)+len(e.Times) != 0 {
		t.Errorf("no history: %+v", e)
	}
}
