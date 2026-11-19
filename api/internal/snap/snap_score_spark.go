package snap

import "github.com/shifter-io/atlas/api/internal/jsnum"

// ScorePoint is one point of a network window's atlas_score_spark
// (ATLAS-SCORE-SPARK-1, calculated/score_history.py): the balanced score
// (Score, unrounded; NaN when the build published none) of the slot ending
// At, an hour for the 24h window and a UTC day for the 7d and 30d windows, as
// the slot's last published build published it. Stored is false when no
// published build's score was found in the slot (before the history began).
type ScorePoint struct {
	At     string `json:"at"`
	Score  Num    `json:"score"`
	Stored bool   `json:"stored"`
	// EarlierRule marks a point an earlier methodology's build scored, kept
	// where the build's own rule has none (SCORE-HISTORY-2).
	EarlierRule bool `json:"earlier_rule"`
}

func (v *ScorePoint) UnmarshalJSON(d []byte) error {
	type plain ScorePoint
	return jsnum.DecodeNA(d, (*plain)(v))
}
