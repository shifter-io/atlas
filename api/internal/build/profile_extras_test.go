package build

import (
	"maps"
	"math"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/shifter-io/atlas/api/internal/model"
	"github.com/shifter-io/atlas/api/internal/snap"
	"github.com/shifter-io/atlas/api/internal/store"
	"github.com/shifter-io/atlas/api/internal/testscale"
)

// extrasBuild is a synthetic build (three measured networks, one unmeasured)
// with net00's prices, trial, claimed pool and scope, reviews, community runs
// and editorial tape rows of two kinds, after edit has changed its snapshot.
func extrasBuild(t *testing.T, edit func(doc map[string]any)) (*Result, *store.Inputs) {
	t.Helper()
	in, err := testscale.Inputs(testscale.Shape{Networks: 3, Countries: 1, Exits: 3, Tail: 5, Unmeasured: 1, Events: 5, Browser: true})
	if err != nil {
		t.Fatal(err)
	}
	if edit != nil {
		in.Snapshot = editSnapshot(t, in.Snapshot, edit)
	}
	f := func(v float64) *float64 { return &v }
	s := func(v string) *string { return &v }
	claimed := int64(1_000_000)
	in.Pricing = append(in.Pricing,
		store.PricingRow{ProviderSlug: "net00", Category: "residential", PricePerGb: f(4), PricePerGbAtScale: f(3), Trial: s("Free trial, card required")},
		store.PricingRow{ProviderSlug: "net01", Category: "residential", PricePerGb: f(1)})
	in.Trust = append(in.Trust, store.TrustRow{ProviderSlug: "net00", Category: "residential", ClaimedPoolSize: &claimed})
	in.Scopes = append(in.Scopes, store.ScopeRow{Kind: "residential_rotating", Summary: s("Rotating exits sampled")})
	at := in.ReadAt.Add(-time.Hour)
	for i, r := range []struct{ product, verification, useCase string }{
		{"residential_rotating", "run_verified", "Web scraping"}, {"residential_sticky", "invoice_verified", "price-monitoring"},
		{"datacenter", "run_verified", "gaming"}, {"residential_rotating", "pending", ""},
	} {
		in.Reviews = append(in.Reviews, store.ReviewRow{ID: "r" + r.product + r.verification, ProviderSlug: "net00", Rating: int16(5 - i),
			ProductKind: r.product, Verification: r.verification, UseCase: r.useCase, PostedAt: at})
	}
	for _, r := range []struct{ product, status, reason string }{{"residential_rotating", "accepted", ""}, {"mobile", "rejected", "Too few requests"}} {
		run := store.CommunityRunRow{ID: "u" + r.product, ProviderSlug: "net00", ProductKind: r.product, Status: r.status, SubmittedAt: at}
		if r.reason != "" {
			run.RejectionReason = &r.reason
		}
		in.CommunityRuns = append(in.CommunityRuns, run)
	}
	newest := in.ReadAt
	for _, e := range in.Events {
		if e.At.After(newest) {
			newest = e.At
		}
	}
	in.Tape = append(in.Tape, store.TapeRow{ID: "t-res", Kind: "rotation", ProviderSlug: "net00", Product: "residential", OccurredAt: newest.Add(time.Minute)},
		store.TapeRow{ID: "t-dc", Kind: "audit", ProviderSlug: "net00", Product: "datacenter", OccurredAt: at})
	res, err := Build(in)
	if err != nil {
		t.Fatal(err)
	}
	return res, in
}

// setPeers ranks every balanced score second of peers (every component's
// ready networks of its kind).
func setPeers(doc map[string]any, peers int) {
	for _, p := range doc["products"].([]any) {
		for _, w := range p.(map[string]any)["windows"].(map[string]any) {
			s := w.(map[string]any)["scores"].(map[string]any)["balanced"].(map[string]any)
			s["rank"] = 2
			for _, c := range s["components"].(map[string]any) {
				c.(map[string]any)["inputs"] = map[string]any{"peers": peers}
			}
		}
	}
}

func TestTheNetworkAnswerCarriesItsExtras(t *testing.T) {
	t.Parallel()
	res, _ := extrasBuild(t, func(doc map[string]any) {
		setPeers(doc, 3)
		setPricing(doc, "net00", 4, 3, 3)
		setPricing(doc, "net01", 1, math.NaN(), 1)
	})
	pg := res.Pages
	ans := pg.Network["net00/residential"]
	p := ans.Profile
	if p.Score.Peers != 3 || p.Score.Standing.Peers != 3 || p.Score.Rank != 2 || p.Score.Standing.Rank != 2 {
		t.Errorf("score %+v", p.Score)
	}
	if u := pg.Network["net03/residential"].Profile.Score; u.Peers.OK() || u.Standing.Rank.OK() {
		t.Errorf("unmeasured score %+v", u)
	}
	if p.Pricing.ListRate != 3 || p.Pricing.HasTrial == nil || !*p.Pricing.HasTrial || model.SV(p.Pricing.TrialKind) != "card" || p.Pricing.PricePerGb != 4 {
		t.Errorf("pricing %+v", p.Pricing)
	}
	pr := p.Protocol
	if len(pr.Targets) == 0 {
		t.Errorf("protocol %+v of %d targets", pr, len(p.Targets))
	}
	for _, id := range pr.Targets {
		if i := slices.IndexFunc(p.Targets, func(tb ProfileTarget) bool { return tb.Target == id }); i < 0 || !(p.Targets[i].Requests > 0) {
			t.Errorf("protocol target %s", id)
		}
	}
	for _, r := range Ranges {
		st := ans.Stats[r.ID]
		if st.ClaimShare != st.UniqueIps/1_000_000 || !st.ClaimShare.OK() {
			t.Errorf("%s claim share %v of %v IPs", r.ID, st.ClaimShare, st.UniqueIps)
		}
		rows := pg.RankingsByKind[r.ID+"/residential"].Networks
		succeeded := 0
		for _, row := range rows {
			if row.SuccessRate.OK() {
				succeeded++
			}
		}
		if want := rowRanks(rows)["net00"]; st.Ranks != want || !st.Ranks.SuccessRate.Rank.OK() || st.Ranks.SuccessRate.Peers != succeeded ||
			succeeded < 3 || st.Ranks.ListRate != (model.Rank{Rank: 2, Peers: 2}) {
			t.Errorf("%s ranks %+v, want %+v", r.ID, st.Ranks, want)
		}
		if want, _ := alternative(rowOf(t, pg.RankingsByKind[r.ID+"/residential"], "net00"), rows); (st.Alternative == nil) != (want == nil) ||
			want != nil && *st.Alternative != *want {
			t.Errorf("%s alternative %+v, want %+v", r.ID, st.Alternative, want)
		}
	}
	if pg.Network["net01/residential"].Stats["30d"].ClaimShare.OK() {
		t.Error("a claim share without a claimed pool")
	}
}

// With ?kind=, the records and the review summary are the kind's only.
func TestTheNetworkAnswerRecordsAreOfItsKind(t *testing.T) {
	t.Parallel()
	res, _ := extrasBuild(t, nil)
	ans := res.Pages.Network["net00/residential"]
	if ans.Records.Reviews != 2 || len(ans.Reviews) != 2 || ans.Records.CommunityRuns != 1 || ans.CommunityRuns[0].Product != "residential_rotating" {
		t.Fatalf("records %+v reviews %+v", ans.Records, ans.Reviews)
	}
	for _, r := range ans.Tape {
		if r.Product != "residential" || r.ID == "t-dc" {
			t.Errorf("tape row %+v", r)
		}
	}
	if ans.Records.Tape != len(ans.Tape) || ans.Tape[0].ID != "t-res" {
		t.Errorf("tape %d of %d, first %+v", len(ans.Tape), ans.Records.Tape, ans.Tape[0])
	}
	rv := ans.Profile.Reviews
	if rv.Count != 2 || rv.VerifiedCount != 2 || rv.RunVerifiedCount != 1 || rv.Mean != 4.5 || rv.Distribution != [5]Num{0, 0, 0, 1, 1} {
		t.Errorf("review summary %+v", rv)
	}
	// A review's use case and a run's rejection reason are codes, null when
	// the text is none (PROFILE-TEXT-REMOVE-1).
	useCases := map[string]string{}
	for _, r := range res.Dataset.Reviews {
		useCases[r.ID] = model.SV(r.UseCase)
	}
	if want := map[string]string{"rresidential_rotatingrun_verified": "web_scraping", "rresidential_stickyinvoice_verified": "price_monitoring",
		"rdatacenterrun_verified": "", "rresidential_rotatingpending": ""}; !maps.Equal(useCases, want) {
		t.Errorf("use cases %v, want %v", useCases, want)
	}
	for _, r := range res.Dataset.CommunityRuns {
		if want := map[string]string{"umobile": "too_few_requests"}[r.ID]; model.SV(r.RejectionReason) != want {
			t.Errorf("run %s rejection reason %v, want %q", r.ID, r.RejectionReason, want)
		}
	}
	// The provider's own summary still counts every product.
	if res.Details["net00"].Provider.Reviews.Count != 3 {
		t.Errorf("provider reviews %+v", res.Details["net00"].Provider.Reviews)
	}
}

// One tape in time order, newest first: editorial rows dated and marked.
func TestTheTapeIsInTimeOrder(t *testing.T) {
	t.Parallel()
	res, in := extrasBuild(t, nil)
	tape := res.Dataset.Tape
	if len(tape) < 3 || tape[0].ID != "t-res" || tape[0].Source != model.SourceEditorial || tape[0].Event != "rotation" ||
		model.SV(tape[0].At) != JSISO(in.Tape[0].OccurredAt) {
		t.Fatalf("tape of %d events: first %+v", len(in.Events), tape[0])
	}
	measured := 0
	for i, r := range tape {
		if i > 0 && snap.Ms(model.SV(r.At)) > snap.Ms(model.SV(tape[i-1].At)) {
			t.Errorf("row %d (%s) is newer than the one before it", i, model.SV(r.At))
		}
		if r.Source == model.SourceMeasured {
			measured++
			if r.Event == "" || strings.Contains(r.Event, "estimat") {
				t.Errorf("measured row %+v: raw event kind %q", r, r.Event)
			}
		}
	}
	if measured != len(tape)-2 || measured == 0 {
		t.Errorf("%d measured rows of %d", measured, len(tape))
	}
	if tapeEvent("estimate_hour_released") != "hour_released" || tapeEvent("ip_overlap") != "ip_overlap" {
		t.Error("raw event kinds")
	}
}

// Alone in its kind: #1 of 1, ranked first of one, no alternative.
func TestANetworkAloneInItsKind(t *testing.T) {
	t.Parallel()
	res, _ := extrasBuild(t, func(doc map[string]any) {
		setKind(doc, "net01", "datacenter")
		setAlone(doc, "net01")
	})
	ans := res.Pages.Network["net01/datacenter"]
	sc := ans.Profile.Score
	if sc.Rank.OK() || sc.Standing.Rank != 1 || sc.Standing.Peers != 1 || sc.Peers != 1 {
		t.Errorf("standing %+v", sc.Standing)
	}
	for _, r := range Ranges {
		st := ans.Stats[r.ID]
		if st.Ranks.SuccessRate != (model.Rank{Rank: 1, Peers: 1}) || st.Alternative != nil {
			t.Errorf("%s ranks %+v alternative %+v", r.ID, st.Ranks, st.Alternative)
		}
	}
}

func TestRowRanksShareTies(t *testing.T) {
	t.Parallel()
	row := func(slug string, success, uptime, dc Num, value int, list Num) RankingRow {
		r := RankingRow{Slug: slug, SuccessRate: success, UptimePct: uptime,
			RowPricing: RowPricing{ListRate: list}, WindowFigures: WindowFigures{DatacenterAsnShare: dc}}
		if value > 0 {
			r.ValueRankAllTargets = &value
		}
		return r
	}
	rows := []RankingRow{row("a", 0.9, 99, 0.1, 2, 3), row("b", 0.9, 98, 0.1, 1, nan), row("c", 0.8, nan, 0.2, 2, 1), row("d", 0.7, 97, 0.3, 0, 2)}
	got := rowRanks(rows)
	a, b, c, d := got["a"], got["b"], got["c"], got["d"]
	r := func(rank Num, peers int) model.Rank { return model.Rank{Rank: rank, Peers: peers} }
	if a.SuccessRate != r(1, 4) || b.SuccessRate != r(1, 4) || c.SuccessRate != r(3, 4) {
		t.Errorf("success %+v %+v %+v", a.SuccessRate, b.SuccessRate, c.SuccessRate)
	}
	if a.UptimePct != r(1, 3) || b.UptimePct != r(2, 3) || c.UptimePct.Rank.OK() || c.UptimePct.Peers != 3 {
		t.Errorf("uptime %+v %+v %+v", a.UptimePct, b.UptimePct, c.UptimePct)
	}
	// Lower is better: hosting share and list rate. The value rank is the
	// row's valueRankAllTargets among the rows with one.
	if a.DatacenterAsnShare != r(1, 4) || c.DatacenterAsnShare != r(3, 4) || b.ValueRankAllTargets != r(1, 3) ||
		a.ValueRankAllTargets != r(2, 3) || c.ValueRankAllTargets != r(2, 3) || d.ValueRankAllTargets.Rank.OK() ||
		d.ValueRankAllTargets.Peers != 3 || c.ListRate != r(1, 3) || a.ListRate != r(3, 3) || b.ListRate.Rank.OK() {
		t.Errorf("lower first %+v %+v %+v %+v", a, b, c, d)
	}
	// A published rank carries the count it ranks among.
	rows[0].valueRanked = 18
	if v := rowRanks(rows)["a"].ValueRankAllTargets; v != r(2, 18) {
		t.Errorf("published value rank %+v", v)
	}
	if n := noRanks(); n.SuccessRate.Rank.OK() || n.ListRate.Peers != 0 {
		t.Errorf("no ranks %+v", n)
	}
}

// The alternative compares the all-targets bill (HTTP-COST-RETIRE-1); the
// plain-HTTP cost, in the opposite order here, is not read.
func TestTheAlternativeIsTheFirstCheaperNetworkAsGood(t *testing.T) {
	t.Parallel()
	row := func(slug string, success, cost Num) RankingRow {
		r := RankingRow{Slug: slug, Name: "N " + slug, SuccessRate: success, CostPer1k: 10 - cost, LatencyP50: 100}
		r.CostPer1kAllTargets = cost
		return r
	}
	own := row("own", 0.9, 2)
	rows := []RankingRow{row("dear", 0.99, 3), row("worse", 0.87, 1), own, row("unpriced", 0.99, nan), row("alt", 0.885, 1.5), row("cheapest", 0.95, 1)}
	got, why := alternative(own, rows)
	if why != "" || got == nil || got.Slug != "alt" || got.Name != "N alt" || got.SuccessRate != 0.885 || got.LatencyP50 != 100 || got.CheaperBy != 0.25 {
		t.Fatalf("alternative %+v", got)
	}
	if got.SuccessScore != nil {
		t.Errorf("alternative without a score: %+v", got.SuccessScore)
	}
	// The alternative carries its own ranking row's success score.
	rows[4].WindowFigures.SuccessScore = &model.SuccessScore{MetricScore: model.MetricScore{Score: 7, Rank: 2, RankedOf: 4, Status: scoreRanked}}
	if a, _ := alternative(own, rows); a.SuccessScore == nil || *a.SuccessScore != (model.MetricScore{Score: 7, Rank: 2, RankedOf: 4, Status: scoreRanked}) {
		t.Errorf("alternative score %+v", a.SuccessScore)
	}
	// Without a cheaper network as good, or without its own cost: none, with
	// the reason (ALT-REASON-1).
	if a, why := alternative(rows[5], rows); a != nil || why != NANoCheaperPeer {
		t.Errorf("the cheapest has alternative %+v (%q)", a, why)
	}
	if a, why := alternative(rows[3], rows); a != nil || why != NAIncompleteTargets {
		t.Errorf("a network without a bill has alternative %+v (%q)", a, why)
	}
	// Own lower-bound bill is compared as is; a peer's lower bound is no
	// alternative, its true bill may be higher.
	yes, no := true, false
	rows[2].CostIsLowerBound, rows[4].CostIsLowerBound = &yes, &no
	if a, _ := alternative(rows[2], rows); a == nil || a.Slug != "alt" {
		t.Errorf("a lower-bound bill's alternative %+v", a)
	}
	rows[4].CostIsLowerBound = &yes
	if a, _ := alternative(own, rows); a == nil || a.Slug != "cheapest" {
		t.Errorf("a lower-bound peer is the alternative %+v", a)
	}
}
