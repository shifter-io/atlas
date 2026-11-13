package build

import (
	"encoding/json"
	"strings"
	"testing"
	"time"

	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/Zeus-Development/proxy-index/api/internal/store"
	"github.com/Zeus-Development/proxy-index/api/internal/testscale"
)

var recordsBase = time.Date(2026, 1, 1, 12, 0, 0, 0, time.UTC)

func hoursBefore(h int) time.Time { return recordsBase.Add(-time.Duration(h) * time.Hour) }

// recordInputs is a two-network build with every kind of site record:
// scopes, weights, profiles, reviews, community runs, changelog and
// editorial tape rows; its two newest events share their time.
func recordInputs(t *testing.T) *store.Inputs {
	t.Helper()
	in, err := testscale.Inputs(testscale.Shape{Networks: 2, Countries: 1, Exits: 3, Tail: 30, Events: 5})
	if err != nil {
		t.Fatal(err)
	}
	in.Events[1].At = in.Events[0].At
	in.Scopes = []store.ScopeRow{{Kind: "daily", AllTargets: true}, {Kind: "pulse", TargetIDs: []string{"t1", "t2"}}}
	in.Weights = []store.WeightRow{{Component: "speed", Weight: 0.25}}
	in.Profiles = []store.ProfileRow{{ID: "fast", Label: sp("Fast"), Weights: json.RawMessage(`{"speed":0.75}`)}, {ID: "plain"}}
	review := func(id, slug string, rating int16, verification string, h int) store.ReviewRow {
		return store.ReviewRow{ID: id, ProviderSlug: slug, Rating: rating, Verification: verification, PostedAt: hoursBefore(h)}
	}
	in.Reviews = []store.ReviewRow{review("r-old", "net00", 4, "run_verified", 48), review("r-new", "net00", 5, "invoice_verified", 0),
		review("r-pending", "net00", 1, "pending", 1), review("r-self", "net00", 2, "self_reported", 2),
		review("r-other", "net01", 3, "pending", 72)}
	in.CommunityRuns = []store.CommunityRunRow{
		{ID: "c1", ProviderSlug: "net00", Status: "accepted", Targets: json.RawMessage(`[]`), SuccessSeries: json.RawMessage(`null`), SubmittedAt: hoursBefore(1)},
		{ID: "c2", ProviderSlug: "net01", Status: "rejected", SubmittedAt: hoursBefore(0)}}
	in.Changelog = []store.ChangeRow{{ID: "ch1", ProviderSlug: sp("net00"), OccurredAt: hoursBefore(2)},
		{ID: "ch2", ProviderSlug: sp("gone"), OccurredAt: hoursBefore(1)}, {ID: "ch3", OccurredAt: hoursBefore(0)}}
	in.Tape = []store.TapeRow{{ID: "ed-old", ProviderSlug: "net00", OccurredAt: hoursBefore(3)}, {ID: "ed-new", ProviderSlug: "net01", OccurredAt: hoursBefore(0)}}
	return in
}

func recordIDs[T any](rows []T, id func(T) string) string {
	out := []string{}
	for _, r := range rows {
		out = append(out, id(r))
	}
	return strings.Join(out, " ")
}

func reviewID(r model.UserReview) string   { return r.ID }
func runID(r model.CommunityRun) string    { return r.ID }
func changeID(r model.ChangeEvent) string  { return r.ID }
func tapeEventID(r model.TapeEvent) string { return r.ID }

// The site records are ordered newest first, counted in the totals and the
// review summaries, and handed to their network's detail.
func TestBuildFillsTheSiteRecords(t *testing.T) {
	res, err := Build(recordInputs(t))
	if err != nil {
		t.Fatal(err)
	}
	ds := res.Dataset
	if got := recordIDs(ds.Reviews, reviewID); got != "r-new r-pending r-self r-old r-other" {
		t.Errorf("reviews %s", got)
	}
	if got := recordIDs(ds.CommunityRuns, runID); got != "c2 c1" {
		t.Errorf("community runs %s", got)
	}
	if c1 := ds.CommunityRuns[1]; c1.Targets == nil || len(c1.Targets) != 0 || c1.SuccessSeries == nil || c1.LatencySeries == nil {
		t.Errorf("c1 series: %+v", c1)
	}
	if got := recordIDs(ds.Changelog, changeID); got != "ch3 ch2 ch1" {
		t.Errorf("changelog %s", got)
	}
	net00 := res.Details["net00"]
	if net00 == nil {
		t.Fatalf("no net00 detail: %v", res.Details)
	}
	if model.SV(ds.Changelog[1].ProviderName) != "gone" || ds.Changelog[0].ProviderName != nil ||
		model.SV(ds.Changelog[2].ProviderName) != net00.Provider.Name || ds.CommunityRuns[1].ProviderName != net00.Provider.Name {
		t.Errorf("provider names: changelog %+v, run %q", ds.Changelog, ds.CommunityRuns[1].ProviderName)
	}
	n := len(ds.Tape)
	if n < 4 || ds.Tape[0].ID != "event-5" || ds.Tape[1].ID != "event-4" || ds.Tape[n-2].ID != "ed-new" || ds.Tape[n-1].ID != "ed-old" {
		t.Errorf("tape %s", recordIDs(ds.Tape, tapeEventID))
	}
	if ds.Totals.Reviews != 3 || ds.Totals.VerifiedReviews != 2 || ds.Totals.CommunityRuns != 1 {
		t.Errorf("totals reviews %v, verified %v, runs %v", ds.Totals.Reviews, ds.Totals.VerifiedReviews, ds.Totals.CommunityRuns)
	}
	sum := net00.Provider.Reviews
	if sum.Count != 3 || sum.VerifiedCount != 2 || sum.Mean != 3.67 || sum.VerifiedMean != 4.5 || sum.Distribution != [5]Num{0, 1, 0, 1, 1} {
		t.Errorf("net00 review summary %+v", sum)
	}
	if other := res.Details["net01"].Provider.Reviews; other.Count != 0 || other.Mean.OK() || other.VerifiedMean.OK() {
		t.Errorf("net01 review summary %+v", other)
	}
	if recordIDs(net00.Reviews, reviewID) != "r-new r-self r-old" || recordIDs(net00.CommunityRuns, runID) != "c1" ||
		recordIDs(net00.Changelog, changeID) != "ch1" || !strings.Contains(recordIDs(net00.Tape, tapeEventID), "ed-old") {
		t.Errorf("net00 detail: reviews %s, runs %s, changelog %s, tape %s", recordIDs(net00.Reviews, reviewID),
			recordIDs(net00.CommunityRuns, runID), recordIDs(net00.Changelog, changeID), recordIDs(net00.Tape, tapeEventID))
	}
	recs := res.Coverage.Records
	if recs["reviews"].Count != 5 || recs["communityRuns"].Count != 2 || recs["changelog"].Count != 3 || recs["tape"].Count != n {
		t.Errorf("coverage records %+v", recs)
	}
}

// A scope over every target runs the measured targets, the other its own
// list; the profiles keep their weights; both are in the coverage report.
func TestBuildFillsTheScopesAndProfiles(t *testing.T) {
	in := recordInputs(t)
	res, err := Build(in)
	if err != nil {
		t.Fatal(err)
	}
	ds := res.Dataset
	in.Scopes[1].TargetIDs[0] = "changed"
	measured := []string{}
	for _, tg := range ds.Targets {
		if tg.Source == model.SourceMeasured {
			measured = append(measured, tg.ID)
		}
	}
	if len(ds.Scopes) != 2 || len(measured) == 0 || strings.Join(ds.Scopes[0].Targets, " ") != strings.Join(measured, " ") {
		t.Fatalf("scopes %+v, measured targets %v", ds.Scopes, measured)
	}
	if got := strings.Join(ds.Scopes[1].Targets, " "); got != "t1 t2" {
		t.Errorf("pulse scope targets %v", got)
	}
	if len(ds.Profiles) != 2 || model.SV(ds.Profiles[0].Label) != "Fast" || ds.Profiles[0].Weights.Speed != 0.75 ||
		ds.Profiles[0].Weights.Value.OK() || ds.Profiles[1].Weights.Speed.OK() {
		t.Errorf("profiles %+v", ds.Profiles)
	}
	cov := res.Coverage
	if len(cov.Scopes) != 2 || cov.Scopes[1].ID != "pulse" || len(cov.Profiles) != 2 || cov.Profiles[0].ID != "fast" || cov.Profiles[0].Name != "Fast" {
		t.Errorf("coverage scopes %+v, profiles %+v", cov.Scopes, cov.Profiles)
	}
}

// A record the build cannot read fails the build and names the record.
func TestBuildFailsOnARecordItCannotRead(t *testing.T) {
	cases := []struct {
		name  string
		spoil func(in *store.Inputs)
		want  string
	}{
		{"snapshot", func(in *store.Inputs) { in.Snapshot = &store.SnapshotRow{BuildID: 7, Payload: []byte("not json")} },
			"published snapshot 7: "},
		{"profile weights", func(in *store.Inputs) { in.Profiles[0].Weights = json.RawMessage(`[1]`) }, "profile fast weights: "},
		{"run targets", func(in *store.Inputs) { in.CommunityRuns[0].Targets = json.RawMessage(`{`) }, "community run c1 targets: "},
		{"run success series", func(in *store.Inputs) { in.CommunityRuns[0].SuccessSeries = json.RawMessage(`{}`) },
			"community run c1 success_series: "},
		{"run latency series", func(in *store.Inputs) { in.CommunityRuns[0].LatencySeries = json.RawMessage(`"x"`) },
			"community run c1 latency_series: "},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			in := recordInputs(t)
			c.spoil(in)
			if res, err := Build(in); err == nil || !strings.Contains(err.Error(), c.want) || res != nil {
				t.Fatalf("Build = %v, %v; want an error with %q", res, err, c.want)
			}
		})
	}
}

// A likely reseller is counted once per network in the fleet totals.
func TestScanFleetCountsResellersAndRecentIncidents(t *testing.T) {
	reseller := &model.Provider{Slug: "a"}
	reseller.Overlap.LikelyUpstream = &model.LikelyUpstream{}
	reseller.Incidents = []model.Incident{{StartedAgoSeconds: 3600}, {StartedAgoSeconds: 86401}}
	s := scanFleet([]*entry{{P: reseller}, {P: &model.Provider{Slug: "b"}}})
	if s.resellers != 1 || s.incidents != 1 {
		t.Fatalf("resellers %d, incidents %d", s.resellers, s.incidents)
	}
}

// A network without measurements of a kind has no view of it, and an unknown
// range id reads as the widest range.
func TestNoProductViewAndTheDefaultRange(t *testing.T) {
	if v := providerForProduct(&model.Provider{Slug: "a"}, "mobile"); v != nil {
		t.Errorf("view %+v", v)
	}
	if r := rangeMeta("90d"); r.ID != Ranges[len(Ranges)-1].ID || r.ID != "30d" {
		t.Errorf("range %+v", r)
	}
}
