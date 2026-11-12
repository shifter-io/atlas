package build

import "github.com/Zeus-Development/proxy-index/api/internal/model"

// runScores sets every community run's successScore: its success rate
// ranked with metricScores, as a network row's, among the measured networks
// that sell the run's kind, on that kind's 30-day success rate and requests
// (e.Snap[kind], as the kind's ranking), its own network left out; the
// run's requests are its MIN_REQUESTS count.
func (b *builder) runScores() error {
	for i := range b.ds.CommunityRuns {
		r := &b.ds.CommunityRuns[i]
		kind := runKind(r.Product)
		rated := []ratedRate{}
		for _, e := range b.entries {
			if p := e.Snap[kind]; p != nil && e.P.Slug != r.ProviderSlug {
				m := p.Window("30d").Metrics
				rated = append(rated, ratedRate{m.SuccessRate, m.Completed})
			}
		}
		scores := metricScores(append(rated, ratedRate{r.SuccessRate, r.Requests}))
		r.SuccessScore = &scores[len(scores)-1]
	}
	return nil
}

// runKind is the proxy kind a run's product (such as residential_rotating)
// is of; "" when none.
func runKind(product string) string {
	for _, k := range model.ProxyKinds {
		if productOfKind(product, k) {
			return k
		}
	}
	return ""
}

// runsScore is the medians' success score: absolute, as the market's.
func runsScore(median Num) MarketScore { return absoluteScore(median, MarketScoreBasis) }
