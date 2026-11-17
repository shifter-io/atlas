package build

import (
	"math"
	"testing"
	"time"

	"github.com/shifter-io/atlas/api/internal/model"
	"github.com/shifter-io/atlas/api/internal/snap"
)

// A network's latency of an hour is its one series row there (the
// calculator's merged histograms); an hour with two rows (5-minute rows of an
// older snapshot) or none is null: percentiles are never averaged.
func TestLatencySparkIsTheHoursOneRow(t *testing.T) {
	t.Parallel()
	ax := curveAt(t, "2026-11-17T10:10:00Z", "24h")
	hour, five := time.Hour, 5*time.Minute
	s := latencySpark([]curvePoint{
		point(t, "2026-11-17T08:00:00Z", hour, 640, 1),
		point(t, "2026-11-17T09:00:00Z", five, 500, 1),
		point(t, "2026-11-17T09:05:00Z", five, 900, 1),
	}, ax)
	if len(s.Values) != 24 || s.Values[21] != 640 || s.Values[22].OK() || s.Values[23].OK() || s.Times[21] != 130 {
		t.Errorf("latency %v times %v", s.Values, s.Times)
	}
	if e := latencySpark(nil, ax); e.Values == nil || len(e.Values)+len(e.Times) != 0 {
		t.Errorf("no history: %+v", e)
	}
}

// The market's latency of an hour is the median of the networks' latency
// then, as market.medianLatency is of their range latency; null below 3
// networks with one.
func TestMarketLatencyIsTheMedianOfTheNetworks(t *testing.T) {
	t.Parallel()
	ax := curveAt(t, "2026-11-17T10:10:00Z", "7d")
	lists := [][]Num{
		{nan, 400, 400, nan, nan, nan, 300},
		{nan, 600, 900, nan, nan, nan, 500},
		{nan, 2000, nan, nan, nan, nan, 700},
		{nan, nan, 100, nan, nan, nan, 900},
	}
	s := marketLatency(lists, ax)
	if s.Values[1] != 600 || s.Values[2] != 400 || s.Values[6] != 600 || s.Values[0].OK() || len(s.Times) != 7 {
		t.Errorf("market latency %v", s.Values)
	}
	if two := marketLatency(lists[:2], ax); two.Values[1].OK() {
		t.Errorf("two networks: %v, want null", two.Values[1])
	}
}

// A network's success score of an hour is its one row's point score (the
// calculator's: the window's Success x/10 rule over the hour's daily tests),
// so a steady network's spark reads its headline (#1301); an hour without a
// row, or with several, is not_enough_data; a network without history has
// no spark.
func TestAxisScoresAreTheRowsPointScores(t *testing.T) {
	t.Parallel()
	ax := curveAt(t, "2026-11-17T10:10:00Z", "24h")
	headline := 9.37
	ranked := model.MetricScore{Score: Num(headline), Rank: nan, RankedOf: nan, Status: scoreRanked}
	at := func(s string) time.Time {
		v, err := time.Parse(time.RFC3339, s)
		if err != nil {
			t.Fatal(err)
		}
		return v.Add(2*time.Minute + 30*time.Second)
	}
	h := homeHistory{curve: []curvePoint{point(t, "2026-11-17T08:00:00Z", time.Hour, 0.98, 200)}}
	for hour := 0; hour < 24; hour++ {
		start := ax.start(hour)
		if hour != 5 {
			h.scores = append(h.scores, scorePoint{mid: start.Add(150 * time.Second), score: ranked})
		}
	}
	h.scores = append(h.scores, scorePoint{mid: at("2026-11-17T09:20:00Z"), score: ranked})
	scores := axisScores(h, ax)
	for i, s := range scores {
		switch i {
		case 5, 22:
			if s.Status != notEnoughData || s.Score.OK() {
				t.Errorf("hour %d: %+v, want not_enough_data", i, s)
			}
		default:
			if s.Status != scoreRanked || math.Abs(s.Score.F()-headline) > 0.05 {
				t.Errorf("hour %d: %+v, want the headline %v", i, s, headline)
			}
		}
	}
	if axisScores(homeHistory{}, ax) != nil {
		t.Error("a network without history has a spark")
	}
	row := snap.SeriesRow{SuccessScore: &snap.SeriesScore{Score: nan, Status: "no_ranked_targets"}}
	if s := seriesScore(row); s.Status != "no_ranked_targets" || s.Score.OK() {
		t.Errorf("unranked row %+v", s)
	}
	if s := seriesScore(snap.SeriesRow{}); s.Status != notEnoughData {
		t.Errorf("a row from an older snapshot %+v", s)
	}
}

// The market's success score of an hour is 1 + 9 × the median of the
// networks' trailing success rates there (market.successScore's rule over the
// trailing window ending at the hour, SUCCESS-TRAILING-1): three networks at
// 0.5, 0.9 and 0.95 read 9.1, the median, not their pooled or mean rate; the
// newest point is the headline (each network's newest trailing rate is its
// window rate); an hour without a rate, or a snapshot without trailing rates,
// is null with a null status.
func TestMarketSuccessScoreSparkIsTheMedianOfTheTrailingRates(t *testing.T) {
	t.Parallel()
	ax := curveAt(t, "2026-11-17T10:10:00Z", "24h")
	network := func(at08, newest float64) *entry {
		rate := func(v Num) *Num { return &v }
		rows := []snap.SeriesRow{{At: "2026-11-17T08:00:00Z", TrailingSuccessRate: rate(Num(at08))},
			{At: "2026-11-17T09:00:00Z", TrailingSuccessRate: rate(nan)},
			{At: "2026-11-17T10:00:00Z", TrailingSuccessRate: rate(Num(newest))}}
		w := snap.Window{Series: rows}
		return &entry{P: &model.Provider{PrimaryKind: "residential"}, Snap: map[string]*snap.Product{"residential": {Windows: map[string]snap.Window{"24h": w}}}}
	}
	fleet := []*entry{network(0.5, 0.97), network(0.9, 0.98), network(0.95, 0.99)}
	s := marketScoreSpark(fleetMedianRates(fleet, ax, "24h"))
	if s.Values[21] != 9.1 || model.SV(s.Statuses[21]) != scoreRanked {
		t.Errorf("08:00: %v %v, want 9.1 ranked", s.Values[21], s.Statuses[21])
	}
	if headline := marketScore(Num(median([]float64{0.97, 0.98, 0.99}))); s.Values[23] != headline.Score {
		t.Errorf("newest %v, the headline %v", s.Values[23], headline.Score)
	}
	if s.Values[22].OK() || s.Statuses[22] != nil || len(s.Times) != 24 {
		t.Errorf("09:00: %v %v, want null", s.Values[22], s.Statuses[22])
	}
	old := &entry{P: &model.Provider{PrimaryKind: "residential"}, Snap: map[string]*snap.Product{"residential": {Windows: map[string]snap.Window{"24h": {Series: []snap.SeriesRow{{At: "2026-11-17T08:00:00Z"}}}}}}}
	if o := fleetMedianRates([]*entry{old}, ax, "24h"); o.Values[21].OK() {
		t.Errorf("a snapshot without trailing rates: %v", o.Values[21])
	}
}
