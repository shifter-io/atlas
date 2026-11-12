package build

import "github.com/Zeus-Development/proxy-index/api/internal/snap"

// setRangeScores sets each range's overall success score from the network's
// product of its kind (none without one).
func (a *NetworkAnswer) setRangeScores(sp *snap.Product) {
	if sp == nil {
		return
	}
	for _, r := range Ranges {
		a.Ranges[r.ID].SuccessScore = successScore(sp.Window(r.ID))
	}
}

// addSuccessScores sets the scores the build derives (SUCCESS-SCORE-ALL-1):
// the place table of rated, each network answer's top countries' and each
// day's.
func (pg *Pages) addSuccessScores(rated placeRatings) {
	pg.standings = rated.standings()
	pg.scoreCountries()
	pg.scoreDays()
}

// scoreDays sets each network answer's days[].successScore: the day ranked
// among the networks of its kind with that day (pointScores: MIN_REQUESTS
// being the day's requests). The all-networks answer's days have none.
func (pg *Pages) scoreDays() {
	byKind := map[string]map[string][]ratedPoint{} // kind -> answer key -> its days
	dayAt := map[string]int64{}
	for key, ans := range pg.Network {
		if ans.Kind == AllNetworks || len(ans.Days) == 0 {
			continue
		}
		if byKind[ans.Kind] == nil {
			byKind[ans.Kind] = map[string][]ratedPoint{}
		}
		points := make([]ratedPoint, len(ans.Days))
		for i, d := range ans.Days {
			if _, ok := dayAt[d.Day]; !ok {
				dayAt[d.Day] = int64(len(dayAt))
			}
			points[i] = ratedPoint{at: dayAt[d.Day], rate: ratedRate{d.SuccessRate, d.Requests}}
		}
		byKind[ans.Kind][key] = points
	}
	for _, series := range byKind {
		for key, scores := range pointScores(series) {
			days := pg.Network[key].Days
			for i := range scores {
				s := scores[i]
				days[i].SuccessScore = &s
			}
		}
	}
}
