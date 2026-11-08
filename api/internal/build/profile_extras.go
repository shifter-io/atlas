package build

import (
	"slices"

	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/Zeus-Development/proxy-index/api/internal/snap"
)

// ProductionCadence is how often production tests a network (docs/api-web-map.md
// section 14): one daily test per country, one browser run per target a day.
const ProductionCadence = "One daily test per country and one browser run per target per day"

// ProfileScore is the network's score with the ready networks of its kind it
// was set against (null when unknown) and its standing among them.
type ProfileScore struct {
	model.ScoreView
	Peers    Num           `json:"peers"`
	Standing ScoreStanding `json:"standing"`
}

// ScoreStanding is a ranking row's Standing with every-request.ts
// kindStanding's label: "#1 of 1 in its kind", "#3 of 18 in its kind", "#3"
// or "Unranked".
type ScoreStanding struct {
	Standing
	Note string `json:"note"`
}

// ProfilePricing is the category's pricing with its list rate and trial, as
// a ranking row reads them (RowPricing).
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
// ids of the targets with requests, the product's test scope (site.scopes:
// summary, and its cadence as schedule) and the production cadence.
type Protocol struct {
	Label    model.Str `json:"label"`
	Targets  []string  `json:"targets"`
	Summary  model.Str `json:"summary"`
	Schedule model.Str `json:"schedule"`
	Cadence  string    `json:"cadence"`
}

func profileScore(s model.ScoreView) ProfileScore {
	peers := nan
	if s.Peers.OK() && s.Peers > 0 {
		peers = s.Peers
	}
	standing := Standing{Rank: model.StandingRank(s.Rank, s.Composite, s.Peers), Peers: peers}
	return ProfileScore{ScoreView: s, Peers: peers, Standing: ScoreStanding{Standing: standing, Note: s.KindStanding()}}
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

// measuredSchedule is the schedule part of a measured product's generated
// tagline: what it is measured on.
func measuredSchedule(p *snap.Product) string {
	w := p.Window("30d")
	switch {
	case w.Metrics.EveryRequest():
		return "measured on a census, pulse and load schedule"
	case productCensus(p) != nil || w.Uptime != nil && w.Uptime.Batches.OK() && w.Uptime.Batches > 0:
		return "measured on a census and pulse schedule"
	}
	return "measured HTTP pool sample"
}

// addProtocols sets every network answer's schedule and protocol.
func (pg *Pages) addProtocols(entries []*entry, scopes []model.TestScope) {
	for _, e := range entries {
		for _, kind := range networkRow(e).Kinds {
			ans := pg.Network[e.P.Slug+"/"+kind]
			if ans == nil || ans.Profile == nil {
				continue
			}
			if sp := e.Snap[kind]; sp != nil {
				ans.Profile.Schedule = model.S(measuredSchedule(sp))
			}
			ans.Profile.Protocol = protocol(e.P, kind, ans.Profile.Targets, scopes)
		}
	}
}

func protocol(p *model.Provider, kind string, targets []model.TargetBenchmark, scopes []model.TestScope) Protocol {
	product := p.MeasuredProduct
	if pm := p.ProductMeasurements.Get(kind); pm != nil {
		product = pm.Product
	}
	out := Protocol{Targets: []string{}, Cadence: ProductionCadence}
	if label, ok := model.ProductLabels[product]; ok {
		out.Label = model.S(label)
	}
	for _, t := range targets {
		if t.Requests.OK() && t.Requests > 0 {
			out.Targets = append(out.Targets, t.Target)
		}
	}
	if i := slices.IndexFunc(scopes, func(s model.TestScope) bool { return s.ID == product }); i >= 0 {
		out.Summary, out.Schedule = scopes[i].Summary, scopes[i].Cadence
	}
	return out
}
