package build

import "github.com/Zeus-Development/atlas/api/internal/model"

// runScores sets every community run's successScore: its rate and requests
// ranked with metricScores among the 30-day windows of the run's kind of the
// networks that sell it (e.Snap[kind]), its own network left out.
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
