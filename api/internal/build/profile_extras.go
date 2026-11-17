package build

import "github.com/shifter-io/atlas/api/internal/model"

// ProfileScore is the network's score with the ready networks of its kind it
// was set against (null when unknown) and its standing among them.
type ProfileScore struct {
	model.ScoreView
	Peers    Num      `json:"peers"`
	Standing Standing `json:"standing"`
}

// ProfilePricing is the category's pricing with its list rate (the rate
// costs use, costRate) and trial, as a ranking row reads them (RowPricing).
type ProfilePricing struct {
	model.Pricing
	ListRate  Num        `json:"listRate"`
	HasTrial  model.Bool `json:"hasTrial"`
	TrialKind model.Str  `json:"trialKind"`
}

// ProfileReviews is the review summary of the answer's reviews, with how many
// are backed by a submitted run (verification run_verified).
type ProfileReviews struct {
	model.ReviewSummary
	RunVerifiedCount Num `json:"runVerifiedCount"`
}

// Protocol is how the category was tested: the ids of the targets with
// requests. The tested product is the profile's measuredProduct and its scope
// /v1/meta's (API-PROSE-REMOVAL-1, #962; PROFILE-TEXT-REMOVE-1).
type Protocol struct {
	Targets []string `json:"targets"`
}

func profileScore(s model.ScoreView) ProfileScore {
	peers := nan
	if s.Peers.OK() && s.Peers > 0 {
		peers = s.Peers
	}
	standing := Standing{Rank: model.StandingRank(s.Rank, s.Composite, s.Peers), Peers: peers}
	return ProfileScore{ScoreView: s, Peers: peers, Standing: standing}
}

func profilePricing(p model.Pricing) ProfilePricing {
	row := rowPricing(p)
	return ProfilePricing{Pricing: p, ListRate: row.ListRate, HasTrial: row.HasTrial, TrialKind: row.TrialKind}
}

func profileReviews(slug string, reviews []model.UserReview) ProfileReviews {
	out := ProfileReviews{ReviewSummary: reviewSummary(slug, reviews), RunVerifiedCount: 0}
	for _, r := range reviews {
		if r.Verification == "run_verified" {
			out.RunVerifiedCount++
		}
	}
	return out
}

// ofKind is the rows whose product (a product kind, or a proxy kind) is of
// the proxy kind.
func ofKind[T any](rows []T, kind string, product func(T) string) []T {
	out := []T{}
	for _, r := range rows {
		p := product(r)
		if p == kind || model.ProductCategory[p] == kind {
			out = append(out, r)
		}
	}
	return out
}

// claimShare is the unique IPs ÷ the claimed pool size; NaN without both.
func claimShare(ips, claimed Num) Num {
	if !ips.OK() || !claimed.OK() || claimed <= 0 {
		return nan
	}
	return ips / claimed
}

// attemptsPerSuccess is 1 ÷ rate, the requests one success takes; NaN
// without a rate or at 0.
func attemptsPerSuccess(rate Num) Num {
	if !rate.OK() || rate <= 0 {
		return nan
	}
	return 1 / rate
}

// addProtocols sets every network answer's protocol.
func (pg *Pages) addProtocols(entries []*entry) {
	for _, e := range entries {
		for _, kind := range networkRow(e).Kinds {
			ans := pg.Network[e.P.Slug+"/"+kind]
			if ans == nil || ans.Profile == nil {
				continue
			}
			ans.Profile.Protocol = protocol(ans.Profile.Targets)
		}
	}
}

func protocol(targets []ProfileTarget) Protocol {
	out := Protocol{Targets: []string{}}
	for _, t := range targets {
		if t.Requests.OK() && t.Requests > 0 {
			out.Targets = append(out.Targets, t.Target)
		}
	}
	return out
}
