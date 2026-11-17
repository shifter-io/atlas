package build

import (
	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/snap"
)

// chartPartial is whether a series' oldest and newest points cover only part
// of their UTC hour or day (FLEET-POOL-SERIES-4): the chart answer's
// partialFirst/partialLast and poolPartialFirst/poolPartialLast, and
// market.uniqueIpsCurve's.
type chartPartial struct{ first, last bool }

// partialEnds is a series' chartPartial from its points' times (minutes
// before the as-of time, oldest first; step the minutes of one point's hour
// or day) and has, whether point i holds data: last when the newest point's
// hour or day holds the as-of time (unfinished), first when the oldest point
// with data has only empty points before it in the range (the data starts, or
// resumes, during it). A range whose first point has data reads first false.
func partialEnds(times []Num, has func(int) bool, step float64) chartPartial {
	out := chartPartial{}
	if n := len(times); n > 0 && times[n-1].OK() && times[n-1].F() < step {
		out.last = true
	}
	for i := range times {
		if has(i) {
			out.first = i > 0
			break
		}
	}
	return out
}

// pointMinutes is the minutes of one series point of the range: a UTC hour
// for 24h, a UTC day otherwise (the series' axis, SERIES-HOURLY-DAILY-1).
func pointMinutes(rangeID string) float64 {
	if rangeID == "24h" {
		return 60
	}
	return dayMinutes
}

// rowsPartial is the chartPartial of a window's series rows (age gives a
// row's T), a row holding data when it has requests.
func rowsPartial(rows []snap.SeriesRow, age func(string) Num, rangeID string) chartPartial {
	times := make([]Num, len(rows))
	for i, r := range rows {
		times[i] = age(r.At)
	}
	return partialEnds(times, func(i int) bool { return rows[i].Completed.OK() && rows[i].Completed > 0 }, pointMinutes(rangeID))
}

// pointsPartial is the chartPartial of a chart's points, a point holding
// data when it has a value: the pool series, whose 24h points are whole
// released hours of the hour formula (ipPoints), so false there.
func pointsPartial(points []model.SeriesPoint, rangeID string) chartPartial {
	times := make([]Num, len(points))
	for i, p := range points {
		times[i] = p.T
	}
	return partialEnds(times, func(i int) bool { return points[i].V.OK() }, pointMinutes(rangeID))
}
