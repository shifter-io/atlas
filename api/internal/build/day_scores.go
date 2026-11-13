package build

import (
	"sort"

	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/snap"
)

// DayScoreBasis is a day's successScore basis (DAY-SCORE-1): every target,
// the same rule as the window's successScore.
const DayScoreBasis = "all_targets"

// DayScore is a day's overall success score: the calculation's
// success_overall of the network on that day, ranked among the networks of
// its kind, with the targets it counted (sorted).
type DayScore struct {
	model.MetricScore
	Basis   string   `json:"basis"`
	Targets []string `json:"targets"`
}

// setRangeScores sets each range's overall success score and each day's from
// the network's product of its kind (none without one).
func (a *NetworkAnswer) setRangeScores(sp *snap.Product) {
	if sp == nil {
		return
	}
	for _, r := range Ranges {
		a.Ranges[r.ID].SuccessScore = successScore(sp.Window(r.ID))
	}
	a.setDayScores(sp.Days)
}

// setDayScores sets days[].successScore from the network's days of the same
// date (DAY-SCORE-1); a day the snapshot has no score for keeps none.
func (a *NetworkAnswer) setDayScores(days []snap.Day) {
	byDay := make(map[string]*snap.SuccessOverall, len(days))
	for _, d := range days {
		byDay[dayOf(d.Start)] = d.SuccessOverall
	}
	for i := range a.Days {
		o := byDay[a.Days[i].Day]
		if o == nil {
			continue
		}
		targets := make([]string, 0, len(o.Targets))
		for id := range o.Targets {
			targets = append(targets, id)
		}
		sort.Strings(targets)
		a.Days[i].SuccessScore = &DayScore{MetricScore: model.MetricScore{Score: o.Score, Rank: o.Rank, RankedOf: o.RankedOf, Status: o.Status},
			Basis: DayScoreBasis, Targets: targets}
	}
}

// addSuccessScores sets the scores the build derives (SUCCESS-SCORE-ALL-1):
// the place table of rated and each network answer's top countries'.
func (pg *Pages) addSuccessScores(rated placeRatings) {
	pg.standings = rated.standings()
	pg.scoreCountries()
}
