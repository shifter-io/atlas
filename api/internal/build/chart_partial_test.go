package build

import (
	"encoding/json"
	"strings"
	"testing"
	"time"

	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/snap"
	"github.com/Zeus-Development/atlas/api/internal/testscale"
)

// FLEET-POOL-SERIES-4: the newest point is partial while its hour or day
// holds the as-of time, the oldest point with data when only empty points
// come before it.
func TestPartialEnds(t *testing.T) {
	t.Parallel()
	data := func(from int) func(int) bool { return func(i int) bool { return i >= from } }
	for name, c := range map[string]struct {
		times       []Num
		has         func(int) bool
		step        float64
		first, last bool
	}{
		"data starts on day 3, today 500 min in":    {[]Num{9140, 7700, 6260, 4820, 3380, 1940, 500}, data(3), dayMinutes, true, true},
		"data from the first day, a whole last day": {[]Num{10080, 8640, 7200, 5760, 4320, 2880, 1440}, data(0), dayMinutes, false, false},
		"the current hour, ten minutes in":          {[]Num{1390, 1330, 70, 10}, data(0), 60, false, true},
		"no data":                                   {[]Num{130, 70, 10}, data(9), 60, false, true},
		"no points":                                 {nil, data(0), 60, false, false},
	} {
		if got := partialEnds(c.times, c.has, c.step); got.first != c.first || got.last != c.last {
			t.Errorf("%s: %+v", name, got)
		}
	}
}

// A 7d window's rows (UTC days, the data from the fourth, today 500 minutes
// in) and its pool: the 24h pool of whole released hours is never partial,
// a measured hourly one is at its current hour.
func TestRowsAndPoolPartial(t *testing.T) {
	t.Parallel()
	asOf := time.Date(2026, 11, 17, 8, 20, 0, 0, time.UTC)
	ref := float64(asOf.UnixMilli())
	age := func(at string) Num { return minutesAgo(ref, at) }
	rows := []snap.SeriesRow{}
	for d := range 7 {
		at := time.Date(2026, 11, 11+d, 0, 0, 0, 0, time.UTC).Format(time.RFC3339)
		completed := Num(0)
		if d >= 3 {
			completed = 100
		}
		rows = append(rows, snap.SeriesRow{At: at, Completed: completed})
	}
	if p := rowsPartial(rows, age, "7d"); !p.first || !p.last {
		t.Errorf("7d rows %+v", p)
	}
	released := []model.SeriesPoint{{T: 490, V: 10}, {T: 130, V: 11}, {T: 70, V: 12}}
	measured := []model.SeriesPoint{{T: 130, V: nan}, {T: 70, V: 12}, {T: 10, V: 3}}
	if p := pointsPartial(released, "24h"); p.first || p.last {
		t.Errorf("released hours %+v", p)
	}
	if p := pointsPartial(measured, "24h"); !p.first || !p.last {
		t.Errorf("measured hours %+v", p)
	}
}

// Every chart answer carries the four flags, as its own points give them.
func TestChartAnswersCarryThePartialFlags(t *testing.T) {
	t.Parallel()
	pg := shapePages(t, testscale.Shape{Networks: 2, Countries: 2, Exits: 3, Tail: 5}, nil)
	for _, r := range Ranges {
		c := pg.Charts["net00/residential/"+r.ID]
		if c == nil || len(c.Success) == 0 {
			t.Fatalf("%s: no chart", r.ID)
		}
		times := make([]Num, len(c.Success))
		for i, p := range c.Success {
			times[i] = p.T
		}
		want := partialEnds(times, func(i int) bool { return c.Success[i].V.OK() }, pointMinutes(r.ID))
		pool := pointsPartial(c.Pool, r.ID)
		if c.PartialFirst != want.first || c.PartialLast != want.last || c.PoolPartialFirst != pool.first || c.PoolPartialLast != pool.last {
			t.Errorf("%s: %v %v %v %v, want %+v %+v", r.ID, c.PartialFirst, c.PartialLast, c.PoolPartialFirst, c.PoolPartialLast, want, pool)
		}
		body, _ := json.Marshal(c)
		for _, key := range []string{`"partialFirst":`, `"partialLast":`, `"poolPartialFirst":`, `"poolPartialLast":`} {
			if !strings.Contains(string(body), key) {
				t.Errorf("%s: no %s", r.ID, key)
			}
		}
	}
}
