package build

import (
	"testing"
	"time"

	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/snap"
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

// A network's success score of an hour is its requests-weighted rate ranked
// among the networks of its kind with requests then; an hour without its
// requests is not_enough_data, a network without a snapshot product has no
// rank anywhere and one without history no spark.
func TestAxisPointScoresRankEachHour(t *testing.T) {
	t.Parallel()
	ax := curveAt(t, "2026-11-17T10:10:00Z", "24h")
	measured := func() *entry {
		return &entry{P: &model.Provider{PrimaryKind: "residential"}, Snap: map[string]*snap.Product{"residential": {}}}
	}
	at := func(rate float64) []curvePoint {
		return []curvePoint{point(t, "2026-11-17T08:00:00Z", time.Hour, rate, 200)}
	}
	rows := []RankingRow{{Slug: "a", Kind: "residential"}, {Slug: "b", Kind: "residential"}, {Slug: "c", Kind: "residential"},
		{Slug: "d", Kind: "residential"}, {Slug: "e", Kind: "residential"}}
	bySlug := map[string]*entry{"a": measured(), "b": measured(), "c": measured(), "d": {P: &model.Provider{PrimaryKind: "residential"}}}
	hists := []homeHistory{{curve: at(0.99)}, {curve: at(0.95)}, {curve: at(0.9)}, {curve: at(0.99)}, {}}
	scores := axisPointScores(rows, bySlug, hists, ax)
	a, d := scores["a"], scores["d"]
	if len(a) != 24 || a[21].Status != scoreRanked || a[21].Score != 10 || a[20].Status != notEnoughData || a[20].Score.OK() {
		t.Errorf("a: %+v %+v", a[21], a[20])
	}
	if len(d) != 24 || d[21].Status != notEnoughData {
		t.Errorf("d without a product: %+v", d)
	}
	if _, ok := scores["e"]; ok {
		t.Errorf("e without history has scores")
	}
	if s := scoreSeries(a); len(s.Values) != 24 || s.Values[21] != 10 || model.SV(s.Statuses[20]) != notEnoughData {
		t.Errorf("series %+v", s)
	}
}
