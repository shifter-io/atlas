package build

import (
	"math"

	"github.com/shifter-io/atlas/api/internal/model"
)

// The exclusive score (EXCLUSIVE-SCORE-1): a network's exclusiveIps on the
// 1-10 scale, by its ratio to the leader of its kind in the range, as the
// Atlas Score's ratio-to-leader components: max(1, 10 × exclusiveIps ÷ the
// kind's highest exclusiveIps). The leader reads 10 and a network with no
// exclusive IP 1; rank is by exclusiveIps among the networks of the kind
// that have one, a tie sharing the average place (averageRanks, as success).
// It is display only: no Atlas Score component reads it.

// exclusiveScoreMin is the score of a network with no exclusive IP.
const exclusiveScoreMin = 1

// exclusiveScores sets each network's ExclusiveScore among rows, the
// networks of one kind in one range; a network without exclusiveIps keeps a
// null score (the reason of its exclusiveIps).
func exclusiveScores(rows []OverlapNetwork) {
	scored, values := []int{}, []float64{}
	leader := 0.0
	for i, n := range rows {
		if n.ExclusiveIps.OK() {
			scored, values = append(scored, i), append(values, n.ExclusiveIps.F())
			leader = math.Max(leader, n.ExclusiveIps.F())
		}
	}
	ranks := averageRanks(values)
	for k, i := range scored {
		score := float64(exclusiveScoreMin)
		if leader > 0 {
			score = math.Max(exclusiveScoreMin, bestShownScore*values[k]/leader)
		}
		rows[i].ExclusiveScore = &model.MetricScore{Score: Num(score), Rank: Num(ranks[k]), RankedOf: Num(len(scored)),
			Status: scoreRanked}
	}
}

// addExclusiveFigures sets every measured window's exclusiveIps and
// exclusiveScore from the kind's overlap in the range (OverlapFor): the
// headline windows (stats) and each kind's (details).
func addExclusiveFigures(c *Context, stats map[string]map[string]*WindowStats, details map[string]*ProviderDetail) {
	kinds := map[string]bool{}
	for _, e := range c.Entries {
		for k := range e.Snap {
			kinds[k] = true
		}
	}
	bySlug := entriesBySlug(c.Entries)
	for kind := range kinds {
		for _, r := range Ranges {
			for _, n := range OverlapFor(c, kind, r.ID).Networks {
				if e := bySlug[n.Slug]; e != nil && e.P.PrimaryKind == kind {
					stats[n.Slug][r.ID].setExclusive(n)
				}
				if d := details[n.Slug]; d != nil && d.Windows[kind] != nil {
					d.Windows[kind][r.ID].setExclusive(n)
				}
			}
		}
	}
}

// setExclusive copies n's exclusiveIps and exclusiveScore; ws may be nil.
func (ws *WindowStats) setExclusive(n OverlapNetwork) {
	if ws != nil {
		ws.ExclusiveIps, ws.ExclusiveScore = n.ExclusiveIps, n.ExclusiveScore
	}
}
