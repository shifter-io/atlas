package build

import (
	"encoding/json"
	"fmt"
	"math"
	"sort"
	"strings"
	"time"

	"github.com/Zeus-Development/proxy-index-website/api/internal/jsnum"
	"github.com/Zeus-Development/proxy-index-website/api/internal/model"
	"github.com/Zeus-Development/proxy-index-website/api/internal/snap"
	"github.com/Zeus-Development/proxy-index-website/api/internal/store"
)

// DatasetSchemaVersion is the Dataset contract version this API implements
// (src/lib/types.ts).
const DatasetSchemaVersion = "0.6.0"

// MeasuredTapeRows caps how many measured events the dataset's tape carries.
const MeasuredTapeRows = 200

// Result is every response body of one build, as Go values.
type Result struct {
	Fingerprint store.Fingerprint
	BuiltAt     time.Time
	Dataset     *model.Dataset
	// Details are not served whole (/v1/providers is gone): they feed the
	// network answers and charts.
	Details  map[string]*ProviderDetail
	Search   *SearchIndex
	Coverage *Coverage
	// Pages is the small per-page answers (pages.go); it replaces serving the
	// published snapshot payload whole.
	Pages  *Pages
	Events EventWindow
	// SiteGaps is store.Inputs.SiteGaps: the site tables read as empty
	// because the database lacks them (none once `siteapi migrate` ran).
	SiteGaps []string
}

// JSISO is Date.prototype.toISOString.
func JSISO(t time.Time) string {
	return t.UTC().Format("2006-01-02T15:04:05.000Z")
}

func jsMin(v []float64) float64 {
	m := math.Inf(1)
	for _, x := range v {
		if math.IsNaN(x) {
			return math.NaN()
		}
		if x < m {
			m = x
		}
	}
	return m
}

func jsMax(v []float64) float64 {
	m := math.Inf(-1)
	for _, x := range v {
		if math.IsNaN(x) {
			return math.NaN()
		}
		if x > m {
			m = x
		}
	}
	return m
}

// prepareMedian is scripts/prepare-design-demo.mjs median.
func prepareMedian(v []float64) float64 {
	if len(v) == 0 {
		return math.NaN()
	}
	s := append([]float64{}, v...)
	jsSortNumbers(s)
	return (s[(len(s)-1)/2] + s[len(s)/2]) / 2
}

// fleetTotals ports the totals block of scripts/prepare-design-demo.mjs over
// the fleet basis; review and run counts cover every record.
func fleetTotals(all, fleet []*entry, reviews []model.UserReview, runs []model.CommunityRun) model.FleetTotals {
	sum := func(f func(p *model.Provider) float64) float64 {
		s := 0.0
		for _, e := range fleet {
			s += f(e.P)
		}
		return s
	}
	var t model.FleetTotals
	t.Providers = Num(len(all))
	t.Requests24h = Num(sum(func(p *model.Provider) float64 { return p.Performance.Requests24h.F() }))
	ips := sum(func(p *model.Provider) float64 { return p.Pool.UniqueIps24h.F() })
	t.UniqueIps24h = Num(ips)
	countries := map[string]bool{}
	costs, resCosts, lat, sr := []float64{}, []float64{}, []float64{}, []float64{}
	pairs := map[string]float64{}
	pairOrder := []string{}
	incidents, resellers := 0, 0
	for _, e := range fleet {
		p := e.P
		for _, g := range p.Geo {
			countries[g.CountryCode] = true
		}
		costs = append(costs, p.Pricing.CostPer1kSuccess.F())
		if p.PrimaryKind == "residential" {
			resCosts = append(resCosts, p.Pricing.CostPer1kSuccess.F())
		}
		lat = append(lat, p.Performance.LatencyP50.F())
		sr = append(sr, p.Performance.SuccessRate.F())
		for _, o := range p.Overlap.Overlaps {
			k := []string{p.Slug, o.Slug}
			sort.Strings(k)
			key := strings.Join(k, "|")
			if _, ok := pairs[key]; !ok {
				pairOrder = append(pairOrder, key)
			}
			pairs[key] = o.SharedIps.F()
		}
		for _, inc := range p.Incidents {
			if inc.StartedAgoSeconds <= 86400 {
				incidents++
			}
		}
		if p.Overlap.LikelyUpstream != nil {
			resellers++
		}
	}
	shared := 0.0
	for _, k := range pairOrder {
		shared += pairs[k]
	}
	distinct := math.Max(1, ips-shared)
	if len(fleet) == 0 {
		distinct = math.NaN()
	}
	t.Countries = Num(len(countries))
	t.MedianLatency = Num(prepareMedian(lat))
	t.MedianSuccessRate = Num(prepareMedian(sr))
	t.CheapestCostPer1kSuccess = Num(jsMin(costs))
	t.DearestCostPer1kSuccess = Num(jsMax(costs))
	t.PriceSpreadX = Num(jsMax(costs) / jsMin(costs))
	t.ResidentialSpreadX = Num(jsMax(resCosts) / jsMin(resCosts))
	t.Incidents24h = Num(incidents)
	t.BandwidthGb24h = Num(sum(func(p *model.Provider) float64 { return p.Performance.BandwidthMb24h.F() }) / 1024)
	t.MisclassifiedShare = Num(sum(func(p *model.Provider) float64 {
		return float64(p.Pool.UniqueIps24h.F() * p.Trust.DatacenterAsnShare.F())
	}) / ips)
	t.DistinctIpsAcrossFleet = Num(distinct)
	t.FleetDuplicationShare = Num(1 - distinct/ips)
	t.LikelyResellers = Num(resellers)
	if len(fleet) == 0 {
		t.Requests24h, t.UniqueIps24h, t.Countries, t.Incidents24h, t.LikelyResellers, t.BandwidthGb24h = nan, nan, nan, nan, nan, nan
	}
	published, verified, accepted := 0, 0, 0
	for _, r := range reviews {
		if r.Verification != "pending" {
			published++
		}
		if r.Verification == "invoice_verified" || r.Verification == "run_verified" {
			verified++
		}
	}
	for _, r := range runs {
		if r.Status == "accepted" {
			accepted++
		}
	}
	t.Reviews, t.VerifiedReviews, t.CommunityRuns = Num(published), Num(verified), Num(accepted)
	return t
}

func jsRound2(x float64) Num { return Num(jsnum.Round(x*100) / 100) }

// reviewSummary is generate-demo-data.ts summariseReviews, with an
// unavailable mean (null) instead of 0 when there is nothing to average.
func reviewSummary(slug string, reviews []model.UserReview) model.ReviewSummary {
	s := model.ReviewSummary{Mean: nan, VerifiedMean: nan}
	total, vtotal, n, vn := 0.0, 0.0, 0, 0
	for _, r := range reviews {
		if r.ProviderSlug != slug || r.Verification == "pending" {
			continue
		}
		n++
		total += r.Rating.F()
		if i := int(r.Rating.F()) - 1; i >= 0 && i < 5 {
			s.Distribution[i]++
		}
		if r.Verification == "invoice_verified" || r.Verification == "run_verified" {
			vn++
			vtotal += r.Rating.F()
		}
	}
	s.Count, s.VerifiedCount = Num(n), Num(vn)
	if n > 0 {
		s.Mean = jsRound2(total / float64(n))
	}
	if vn > 0 {
		s.VerifiedMean = jsRound2(vtotal / float64(vn))
	}
	return s
}

func decodeRaw[T any](raw json.RawMessage, what string) ([]T, error) {
	out := []T{}
	if len(raw) == 0 {
		return out, nil
	}
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, fmt.Errorf("%s: %w", what, err)
	}
	if out == nil {
		out = []T{}
	}
	return out, nil
}

// providerForProduct is data.ts providerForProduct.
func providerForProduct(p *model.Provider, kind string) *model.Provider {
	m := p.ProductMeasurements.Get(kind)
	if m == nil {
		return nil
	}
	cp := *p
	cp.PrimaryKind = kind
	cp.MeasuredProduct = m.Product
	cp.Measured = m.Measured
	return &cp
}

// Build assembles every response from one consistent read.
func Build(in *store.Inputs) (*Result, error) {
	res := &Result{Fingerprint: in.Fingerprint, BuiltAt: time.Now().UTC(), Details: map[string]*ProviderDetail{}, SiteGaps: in.SiteGaps}
	var s *snap.Snapshot
	if in.Snapshot != nil {
		s = &snap.Snapshot{}
		if err := json.Unmarshal(in.Snapshot.Full(), s); err != nil {
			return nil, fmt.Errorf("published snapshot %d: %w", in.Snapshot.BuildID, err)
		}
	}
	ds := &model.Dataset{SchemaVersion: DatasetSchemaVersion, Seed: nan, WindowHours: 24}
	ds.GeneratedAt = JSISO(in.ReadAt)
	if s != nil {
		ds.GeneratedAt = s.AsOf
	}
	generated, _ := snap.ParseTime(ds.GeneratedAt)
	if s != nil {
		ds.RunID = model.S("preview-" + s.SnapshotID)
		ds.CollectorVersion = s.ImplementationID
		ds.Snapshot = model.SnapshotMeta{Available: true, SnapshotID: model.S(s.SnapshotID), SchemaVersion: s.SchemaVersion,
			AsOf: model.S(s.AsOf), GeneratedAt: model.S(s.GeneratedAt), RefreshSeconds: s.RefreshSeconds,
			MethodologyID: model.S(s.Methodology.ID), Readiness: model.S(s.Readiness), ContentHash: model.S(in.Snapshot.ContentHash)}
	} else {
		ds.Snapshot = model.SnapshotMeta{SchemaVersion: nan, RefreshSeconds: nan}
	}
	ageOf := func(at time.Time) Num {
		return Num(math.Max(0, jsnum.Round(generated.Sub(at).Seconds())))
	}

	// Targets by sort_order (a framework target's own, else the catalog row's):
	// catalog rows first on a tie, in their order, then framework targets
	// without one. Group and sources come from the snapshot (browser.go).
	frameworkTarget := map[string]store.FrameworkTarget{}
	for _, t := range in.FrameworkTargets {
		frameworkTarget[t.ID] = t
	}
	ds.Targets = []model.TargetMeta{}
	order := map[string]int32{}
	seen := map[string]bool{}
	for _, r := range in.Targets {
		ft, inFramework := frameworkTarget[r.ID]
		t := model.TargetMeta{ID: r.ID, Label: r.Label, Category: r.Category, PassCondition: r.PassCondition, Difficulty: ptrNum(r.Difficulty), Source: "catalog"}
		order[r.ID] = r.SortOrder
		if inFramework {
			t.Source, t.Engine, order[r.ID] = model.SourceMeasured, frameworkEngine(ft), ft.SortOrder
			if t.Label == nil {
				t.Label = ft.Label
			}
			if t.PassCondition == nil {
				t.PassCondition = ft.PassCondition
			}
		}
		ds.Targets = append(ds.Targets, t)
		seen[r.ID] = true
	}
	for _, ft := range in.FrameworkTargets {
		if !seen[ft.ID] {
			ds.Targets = append(ds.Targets, model.TargetMeta{ID: ft.ID, Label: ft.Label, PassCondition: ft.PassCondition, Difficulty: nan,
				Source: model.SourceMeasured, Engine: frameworkEngine(ft)})
			order[ft.ID] = ft.SortOrder
		}
	}
	sort.SliceStable(ds.Targets, func(i, j int) bool { return order[ds.Targets[i].ID] < order[ds.Targets[j].ID] })
	addTargetGroups(ds.Targets, s)
	ds.Scopes = []model.TestScope{}
	for _, r := range in.Scopes {
		var targets any = "all"
		if !r.AllTargets {
			targets = append([]string{}, r.TargetIDs...)
		}
		ds.Scopes = append(ds.Scopes, model.TestScope{ID: r.Kind, Label: r.Label, Summary: r.Summary, Cadence: r.Cadence, Targets: targets,
			LiveIPSampling: r.LiveIPSampling, PoolTracking: r.PoolTracking, OverlapAudit: r.OverlapAudit, StickinessTest: r.Stickiness,
			GeoCoverage: r.GeoCoverage, Rationale: r.Rationale})
	}
	ds.Weights = model.NewBreakdown()
	for _, w := range in.Weights {
		ds.Weights.Set(w.Component, Num(w.Weight))
	}
	if s != nil && len(s.Methodology.Settings.Weights) > 0 {
		// The published calculation's weights are the ones its scores used.
		ds.Weights = model.NewBreakdown()
		for k, v := range s.Methodology.Settings.Weights {
			ds.Weights.Set(k, Num(v))
		}
	}
	ds.Profiles = []model.RankingProfile{}
	for _, r := range in.Profiles {
		p := model.RankingProfile{ID: r.ID, Label: r.Label, Description: r.Description, Weights: model.NewBreakdown()}
		if len(r.Weights) > 0 {
			if err := json.Unmarshal(r.Weights, &p.Weights); err != nil {
				return nil, fmt.Errorf("profile %s weights: %w", r.ID, err)
			}
		}
		ds.Profiles = append(ds.Profiles, p)
	}

	// Providers.
	c := newCatalog(in)
	entries, err := realProviders(in, s, c, ds.Targets)
	if err != nil {
		return nil, err
	}
	addCountryLatencyRanks(entries)
	addRanks(entries)
	names := map[string]string{}
	slugOfFramework := map[string]string{}
	for _, e := range entries {
		names[e.P.Slug] = e.P.Name
		if e.P.Framework != nil {
			slugOfFramework[e.P.Framework.Provider] = e.P.Slug
		}
	}
	nameOrSlug := func(slug string) string {
		if n, ok := names[slug]; ok {
			return n
		}
		return slug
	}

	// Records.
	ds.Reviews = []model.UserReview{}
	for _, r := range in.Reviews {
		ds.Reviews = append(ds.Reviews, model.UserReview{ID: r.ID, ProviderSlug: r.ProviderSlug, Author: r.Author, Role: r.Role, Rating: Num(r.Rating),
			Title: r.Title, Body: r.Body, Product: r.ProductKind, UseCase: r.UseCase, MonthlySpend: r.Spend, MonthsUsed: Num(r.MonthsUsed),
			Verification: r.Verification, InvoiceMonth: r.InvoiceMonth, LinkedRunID: r.LinkedRunID, HelpfulCount: Num(r.HelpfulCount),
			ProviderReply: r.ProviderReply, AgoSeconds: ageOf(r.PostedAt), Source: model.SourceMeasured})
	}
	sortByAge(ds.Reviews, func(r model.UserReview) Num { return r.AgoSeconds })
	ds.CommunityRuns = []model.CommunityRun{}
	for _, r := range in.CommunityRuns {
		run := model.CommunityRun{ID: r.ID, ProviderSlug: r.ProviderSlug, ProviderName: nameOrSlug(r.ProviderSlug), SubmittedBy: r.SubmittedBy,
			Region: r.Region, Product: r.ProductKind, PlanTier: r.PlanTier, CollectorVersion: r.CollectorVersion, Status: r.Status,
			RejectionReason: r.RejectionReason, Requests: ptrNum(r.Requests), SuccessRate: ptrNum(r.SuccessRate), LatencyP50: ptrNum(r.LatencyP50),
			UniqueIps: ptrNum(r.UniqueIps), OverlapShare: ptrNum(r.OverlapShare), DeltaSuccessPts: ptrNum(r.DeltaSuccessPts),
			DeltaLatencyPct: ptrNum(r.DeltaLatencyPct), AgoSeconds: ageOf(r.SubmittedAt), Source: model.SourceMeasured}
		if run.Targets, err = decodeRaw[model.TargetBenchmark](r.Targets, "community run "+r.ID+" targets"); err != nil {
			return nil, err
		}
		if run.SuccessSeries, err = decodeRaw[model.SeriesPoint](r.SuccessSeries, "community run "+r.ID+" success_series"); err != nil {
			return nil, err
		}
		if run.LatencySeries, err = decodeRaw[model.LatencyPoint](r.LatencySeries, "community run "+r.ID+" latency_series"); err != nil {
			return nil, err
		}
		ds.CommunityRuns = append(ds.CommunityRuns, run)
	}
	sortByAge(ds.CommunityRuns, func(r model.CommunityRun) Num { return r.AgoSeconds })
	ds.Changelog = []model.ChangeEvent{}
	for _, r := range in.Changelog {
		ch := model.ChangeEvent{ID: r.ID, Kind: r.Kind, ProviderSlug: r.ProviderSlug, Headline: r.Headline, Detail: r.Detail,
			Delta: ptrNum(r.Delta), Unit: r.Unit, AgoSeconds: ageOf(r.OccurredAt), Severity: r.Severity, Source: model.SourceMeasured}
		if r.ProviderSlug != nil {
			ch.ProviderName = model.S(nameOrSlug(*r.ProviderSlug))
		}
		ds.Changelog = append(ds.Changelog, ch)
	}
	sortByAge(ds.Changelog, func(r model.ChangeEvent) Num { return r.AgoSeconds })

	// Tape: measured events first (newest first), then the editorial rows.
	products := []TapeProduct{}
	if s != nil {
		for _, p := range s.Products {
			// The site's display name (catalog) wins over the snapshot's.
			name := p.Name
			if n, ok := names[slugOfFramework[p.Provider]]; ok {
				name = n
			}
			products = append(products, TapeProduct{Provider: p.Provider, Name: name, Kind: p.Kind})
		}
	}
	for _, fp := range in.FrameworkProducts {
		products = append(products, TapeProduct{Provider: fp.Provider, Name: nameOrSlug(slugOfFramework[fp.Provider]), Kind: fp.Name})
	}
	res.Events = newEventWindow(in)
	tapeEvents := res.Events.measuredTape()
	tapeRef := ds.GeneratedAt
	if s != nil {
		tapeRef = s.GeneratedAt
	}
	sorted := append([]store.EventJSON{}, tapeEvents...)
	sort.SliceStable(sorted, func(i, j int) bool {
		a, b := snap.Ms(sorted[i].At), snap.Ms(sorted[j].At)
		if a != b {
			return a > b
		}
		return sorted[i].ID > sorted[j].ID
	})
	ds.Tape = TapeFromEvents(sorted, products, tapeRef, slugOfFramework)
	editorial := []model.TapeEvent{}
	for _, r := range in.Tape {
		editorial = append(editorial, model.TapeEvent{ID: r.ID, Kind: r.Kind, ProviderSlug: r.ProviderSlug, ProviderName: nameOrSlug(r.ProviderSlug),
			Product: r.Product, Title: r.Title, Detail: r.Detail, Signal: Num(r.Signal), Value: r.Value,
			AgoSeconds: ageOf(r.OccurredAt), Source: model.SourceMeasured})
	}
	sortByAge(editorial, func(r model.TapeEvent) Num { return r.AgoSeconds })
	ds.Tape = append(ds.Tape, editorial...)

	// Review summaries, then the provider list.
	for _, e := range entries {
		e.P.Reviews = reviewSummary(e.P.Slug, ds.Reviews)
	}
	ds.Providers = make([]*model.Provider, len(entries))
	for i, e := range entries {
		ds.Providers[i] = e.P
	}
	ds.Totals = fleetTotals(entries, entries, ds.Reviews, ds.CommunityRuns)
	ds.MeasuredTotals = fleetTotals(entries, entries, nil, nil)

	// Window statistics for every provider (headline) and every category.
	stats := map[string]map[string]*WindowStats{}
	for _, e := range entries {
		stats[e.P.Slug] = map[string]*WindowStats{}
		for _, r := range Ranges {
			stats[e.P.Slug][r.ID] = windowStats(e.P, e.Snap[e.P.PrimaryKind], r.ID)
		}
	}

	ds.MeasuredMarket, err = measuredMarket(entries, stats, ds.MeasuredTotals, ds.Targets)
	if err != nil {
		return nil, err
	}

	// N/A manifests.
	for _, e := range entries {
		declared := append([]string{}, e.Declared...)
		declared = append(declared, "overlap.likelyUpstream")
		for _, k := range e.P.ProductMeasurements.Keys {
			declared = append(declared, "productMeasurements."+k+".overlap.likelyUpstream")
		}
		e.P.NA, err = naOf(e.P, declared, true, "framework")
		if err != nil {
			return nil, err
		}
		e.P.NA = withoutPublishedEmpties(e)
	}
	// After the N/A manifests: a coverage row's nulls are the row's own
	// unknown fields, not gaps of the provider (FIELDS.md).
	addCoverageEvidence(entries, in.Snapshot)
	for i := range ds.Targets {
		if ds.Targets[i].NA, err = naOf(ds.Targets[i], nil, false); err != nil {
			return nil, err
		}
	}
	for i := range ds.Scopes {
		if ds.Scopes[i].NA, err = naOf(ds.Scopes[i], nil, false); err != nil {
			return nil, err
		}
	}
	for i := range ds.Profiles {
		if ds.Profiles[i].NA, err = naOf(ds.Profiles[i], nil, false); err != nil {
			return nil, err
		}
	}
	records := map[string]CoverageRecords{}
	countRecords := func(name string, na []string) {
		r := records[name]
		if r.Fields == nil {
			r.Fields = map[string]int{}
		}
		r.Count++
		r.NACount += len(na)
		for _, p := range na {
			r.Fields[pattern(p)]++
		}
		records[name] = r
	}
	for i := range ds.Reviews {
		if ds.Reviews[i].NA, err = naOf(ds.Reviews[i], nil, false); err != nil {
			return nil, err
		}
		countRecords("reviews", ds.Reviews[i].NA)
	}
	for i := range ds.CommunityRuns {
		if ds.CommunityRuns[i].NA, err = naOf(ds.CommunityRuns[i], nil, false); err != nil {
			return nil, err
		}
		countRecords("communityRuns", ds.CommunityRuns[i].NA)
	}
	for i := range ds.Changelog {
		if ds.Changelog[i].NA, err = naOf(ds.Changelog[i], nil, false); err != nil {
			return nil, err
		}
		countRecords("changelog", ds.Changelog[i].NA)
	}
	for i := range ds.Tape {
		if ds.Tape[i].NA, err = naOf(ds.Tape[i], nil, false); err != nil {
			return nil, err
		}
		countRecords("tape", ds.Tape[i].NA)
	}
	for _, name := range []string{"reviews", "communityRuns", "changelog", "tape"} {
		if _, ok := records[name]; !ok {
			records[name] = CoverageRecords{Fields: map[string]int{}}
		}
	}
	meta := struct {
		SchemaVersion    string               `json:"schemaVersion"`
		GeneratedAt      string               `json:"generatedAt"`
		Seed             Num                  `json:"seed"`
		RunID            model.Str            `json:"runId"`
		CollectorVersion model.Str            `json:"collectorVersion"`
		WindowHours      Num                  `json:"windowHours"`
		Weights          model.Breakdown      `json:"weights"`
		Totals           model.FleetTotals    `json:"totals"`
		MeasuredTotals   model.FleetTotals    `json:"measuredTotals"`
		MeasuredMarket   model.MeasuredMarket `json:"measuredMarket"`
		Snapshot         model.SnapshotMeta   `json:"snapshot"`
	}{ds.SchemaVersion, ds.GeneratedAt, ds.Seed, ds.RunID, ds.CollectorVersion, ds.WindowHours, ds.Weights, ds.Totals, ds.MeasuredTotals, ds.MeasuredMarket, ds.Snapshot}
	// The dataset has no seed by design (only the removed demo fixtures had one).
	meta.Seed = 0
	if ds.NA, err = naOf(meta, []string{}, false); err != nil {
		return nil, err
	}
	res.Dataset = ds

	// Leaderboard: score rank, unranked last (stable).
	leader := append([]*model.Provider{}, ds.Providers...)
	sort.SliceStable(leader, func(i, j int) bool {
		a, b := leader[i].Score.Rank, leader[j].Score.Rank
		if a.OK() != b.OK() {
			return a.OK()
		}
		return a-b < 0
	})

	for _, e := range entries {
		p := e.P
		d := &ProviderDetail{Provider: p, Windows: map[string]map[string]*WindowStats{}, Reviews: []model.UserReview{},
			CommunityRuns: []model.CommunityRun{}, Changelog: []model.ChangeEvent{}, Tape: []model.TapeEvent{}, GeneratedAt: ds.GeneratedAt}
		for _, k := range p.ProductMeasurements.Keys {
			view := providerForProduct(p, k)
			d.Windows[k] = map[string]*WindowStats{}
			for _, r := range Ranges {
				d.Windows[k][r.ID] = windowStats(view, e.Snap[k], r.ID)
			}
		}
		for _, r := range ds.Reviews {
			if r.ProviderSlug == p.Slug && r.Verification != "pending" {
				d.Reviews = append(d.Reviews, r)
			}
		}
		for _, r := range ds.CommunityRuns {
			if r.ProviderSlug == p.Slug {
				d.CommunityRuns = append(d.CommunityRuns, r)
			}
		}
		for _, r := range ds.Changelog {
			if r.ProviderSlug != nil && *r.ProviderSlug == p.Slug {
				d.Changelog = append(d.Changelog, r)
			}
		}
		for _, r := range ds.Tape {
			if r.ProviderSlug == p.Slug {
				d.Tape = append(d.Tape, r)
			}
		}
		res.Details[p.Slug] = d
	}
	res.Pages = buildPages(ds.GeneratedAt, entries, stats, res.Details, s, in.Snapshot)
	res.Pages.Published = publishedProducts(s, entries, in.Providers)
	ds.Pages = datasetPages(res.Pages)
	ds.Lists = datasetLists(res.Pages, s, entries, in.Snapshot)
	res.Search = buildSearchIndex(leader, ds.Targets, ds.GeneratedAt)
	res.Coverage = coverage(ds, res.BuiltAt, records)
	return res, nil
}

// sortByAge orders records newest first (data.ts sorts by agoSeconds ascending), stably.
func sortByAge[T any](rows []T, age func(T) Num) {
	sort.SliceStable(rows, func(i, j int) bool { return age(rows[i])-age(rows[j]) < 0 })
}

func coverage(ds *model.Dataset, builtAt time.Time, records map[string]CoverageRecords) *Coverage {
	cov := &Coverage{GeneratedAt: ds.GeneratedAt, BuiltAt: builtAt.Format(time.RFC3339Nano),
		Summary: CoverageSummary{Providers: len(ds.Providers), ProvidersBySource: map[string]int{model.SourceMeasured: 0},
			NAFieldsBySource: map[string]int{model.SourceMeasured: 0}, SnapshotPublished: ds.Snapshot.Available},
		Dataset: coverageEntity("dataset", "", "", ds.NA, false), Providers: []CoverageEntity{}, Targets: []CoverageEntity{},
		Scopes: []CoverageEntity{}, Profiles: []CoverageEntity{}, Records: records, Fields: map[string]CoverageFieldUse{}}
	cov.Summary.NAFields += len(ds.NA)
	for _, p := range ds.Providers {
		cov.Summary.ProvidersBySource[p.Source]++
		cov.Summary.NAFieldsBySource[p.Source] += len(p.NA)
		cov.Summary.NAFields += len(p.NA)
		cov.Providers = append(cov.Providers, coverageEntity(p.Slug, p.Name, p.Source, p.NA, true))
		seen := map[string]bool{}
		for _, path := range p.NA {
			pt := pattern(path)
			if seen[pt] {
				continue
			}
			seen[pt] = true
			f := cov.Fields[pt]
			f.Providers = append(f.Providers, p.Slug)
			f.Reason = reasonFor(path)
			cov.Fields[pt] = f
		}
	}
	for _, t := range ds.Targets {
		cov.Summary.NAFields += len(t.NA)
		cov.Targets = append(cov.Targets, coverageEntity(t.ID, model.SV(t.Label), t.Source, t.NA, false))
	}
	for _, s := range ds.Scopes {
		cov.Summary.NAFields += len(s.NA)
		cov.Scopes = append(cov.Scopes, coverageEntity(s.ID, model.SV(s.Label), "", s.NA, false))
	}
	for _, p := range ds.Profiles {
		cov.Summary.NAFields += len(p.NA)
		cov.Profiles = append(cov.Profiles, coverageEntity(p.ID, model.SV(p.Label), "", p.NA, false))
	}
	for _, r := range records {
		cov.Summary.NAFields += r.NACount
	}
	return cov
}
