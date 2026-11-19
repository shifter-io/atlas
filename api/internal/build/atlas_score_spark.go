package build

import (
	"github.com/shifter-io/atlas/api/internal/jsnum"
	"github.com/shifter-io/atlas/api/internal/model"
	"github.com/shifter-io/atlas/api/internal/snap"
)

// The Atlas Score trend (ATLAS-SCORE-SPARK-1): atlasScoreSpark beside
// successScoreSpark on the rankings and home rows and the network page's
// ranges, the calculator's points of the network window (snap.ScorePoint):
// the balanced score (the composite that ranks the leaderboard) as the build
// of each hour (24h, 24 points) or the last build of each UTC day (7d and
// 30d, 7 and 30 points) published it, the last point the snapshot's own.

const (
	// atlasNotScored: the slot's build published no Atlas Score for the network.
	atlasNotScored = "not_scored"
	// atlasNoHistory: no published build's score is stored for the slot.
	atlasNoHistory = "no_history"
	// atlasEarlierRule: the slot's score is an earlier methodology's, shown
	// where the build's own rule has none (SCORE-HISTORY-2).
	atlasEarlierRule = "earlier_rule"
	// atlasScoreDecimals rounds the spark's values: the composite at 1 decimal.
	atlasScoreDecimals = 1
)

// AtlasScoreSeries is a range's Atlas Score trend with its times (minutes
// before the build's as-of time), as /v1/networks/{slug} ranges serve it.
type AtlasScoreSeries struct {
	ScoreSeries
	Times []Num `json:"times"`
}

// atlasScoreSpark is a network window's Atlas Score trend: each point's
// score at 1 decimal with its status (ranked, or earlier_rule for an earlier
// methodology's score; not_scored or no_history with a null value) and its time in minutes before the last point's, which is the
// build's as-of time. Empty lists for a window without one (a snapshot from
// before it).
func atlasScoreSpark(w snap.Window) AtlasScoreSeries {
	n := len(w.AtlasScoreSpark)
	s := AtlasScoreSeries{ScoreSeries: ScoreSeries{Values: make([]Num, n), Statuses: make([]model.Str, n)}, Times: make([]Num, n)}
	if n == 0 {
		return s
	}
	last := snap.Ms(w.AtlasScoreSpark[n-1].At)
	for i, p := range w.AtlasScoreSpark {
		s.Values[i], s.Statuses[i], s.Times[i] = nan, model.S(atlasNoHistory), minutesAgo(last, p.At)
		switch {
		case p.Score.OK() && p.EarlierRule:
			s.Values[i], s.Statuses[i] = Num(jsnum.FixedNum(p.Score.F(), atlasScoreDecimals)), model.S(atlasEarlierRule)
		case p.Score.OK():
			s.Values[i], s.Statuses[i] = Num(jsnum.FixedNum(p.Score.F(), atlasScoreDecimals)), model.S(scoreRanked)
		case p.Stored:
			s.Statuses[i] = model.S(atlasNotScored)
		}
	}
	return s
}

// productAtlasSpark is the Atlas Score trend of sp's window of the range;
// empty without a product.
func productAtlasSpark(sp *snap.Product, rangeID string) AtlasScoreSeries {
	if sp == nil {
		return atlasScoreSpark(snap.Window{})
	}
	return atlasScoreSpark(sp.Window(rangeID))
}

// atlasSpark is a ranking row's trend as a home network's: its times kept
// once in the answer's Times (series).
func (a *HomeAnswer) atlasSpark(row RankingRow) HomeScoreSeries {
	i := a.series(SparkSeries{Values: row.AtlasScoreSpark.Values, Times: row.AtlasScoreSparkT}).TimesIndex
	return HomeScoreSeries{ScoreSeries: row.AtlasScoreSpark, TimesIndex: i}
}
