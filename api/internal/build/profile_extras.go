package build

import (
	"slices"

	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/Zeus-Development/proxy-index/api/internal/store"
)

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

// Protocol is how the category was tested: the measured product's label, the
// ids of the targets with requests and the product's test scope's summary
// (site.scopes, which /v1/meta no longer serves as text). Its cadence is
// /v1/meta's scope cadence, in numbers (API-PROSE-REMOVAL-1, #962).
type Protocol struct {
	Label   model.Str `json:"label"`
	Targets []string  `json:"targets"`
	Summary model.Str `json:"summary"`
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

// addProtocols sets every network answer's protocol.
func (pg *Pages) addProtocols(entries []*entry, scopes []store.ScopeRow) {
	for _, e := range entries {
		for _, kind := range networkRow(e).Kinds {
			ans := pg.Network[e.P.Slug+"/"+kind]
			if ans == nil || ans.Profile == nil {
				continue
			}
			ans.Profile.Protocol = protocol(e.P, kind, ans.Profile.Targets, scopes)
		}
	}
}

func protocol(p *model.Provider, kind string, targets []ProfileTarget, scopes []store.ScopeRow) Protocol {
	product := p.MeasuredProduct
	if pm := p.ProductMeasurements.Get(kind); pm != nil {
		product = pm.Product
	}
	out := Protocol{Targets: []string{}}
	if label, ok := model.ProductLabels[product]; ok {
		out.Label = model.S(label)
	}
	for _, t := range targets {
		if t.Requests.OK() && t.Requests > 0 {
			out.Targets = append(out.Targets, t.Target)
		}
	}
	if i := slices.IndexFunc(scopes, func(s store.ScopeRow) bool { return s.Kind == product }); i >= 0 {
		out.Summary = scopes[i].Summary
	}
	return out
}
