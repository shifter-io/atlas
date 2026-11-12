package build

// runScores sets every community run's successScore: its success rate
// ranked with metricScores, as a network row's, among the measured networks
// of its kind on their 30-day success rate and requests, its own network
// left out; the run's requests are its MIN_REQUESTS count.
func (b *builder) runScores() error {
	for i := range b.ds.CommunityRuns {
		r := &b.ds.CommunityRuns[i]
		rated := []ratedRate{}
		for _, e := range b.entries {
			s := b.stats[e.P.Slug]["30d"]
			if e.P.Slug != r.ProviderSlug && s != nil && productOfKind(r.Product, e.P.PrimaryKind) {
				rated = append(rated, ratedRate{s.SuccessRate, s.Requests})
			}
		}
		scores := metricScores(append(rated, ratedRate{r.SuccessRate, r.Requests}))
		r.SuccessScore = &scores[len(scores)-1]
	}
	return nil
}

// runsScore is the medians' success score: absolute, as the market's.
func runsScore(median Num) MarketScore { return absoluteScore(median, MarketScoreBasis) }
