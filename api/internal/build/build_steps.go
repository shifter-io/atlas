package build

import (
	"encoding/json"
	"fmt"
	"sort"

	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/snap"
	"github.com/Zeus-Development/atlas/api/internal/store"
)

// catalog fills the targets, scopes, weights and ranking profiles.
func (b *builder) catalog() error {
	in, ds, s := b.in, b.ds, b.s
	b.targets()
	ds.Scopes = testScopes(in.Scopes, ds.Targets)
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
		p := model.RankingProfile{ID: r.ID, Label: r.Label, Weights: model.NewBreakdown()}
		if len(r.Weights) > 0 {
			if err := json.Unmarshal(r.Weights, &p.Weights); err != nil {
				return fmt.Errorf("profile %s weights: %w", r.ID, err)
			}
		}
		ds.Profiles = append(ds.Profiles, p)
	}
	return nil
}

// targets orders the targets by sort_order (a framework target's own, else
// the catalog row's): catalog rows first on a tie, in their order, then
// framework targets without one. Group and sources come from the snapshot
// (browser.go).
func (b *builder) targets() {
	in, ds := b.in, b.ds
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
	addTargetGroups(ds.Targets, b.s)
}

// providers reads the providers, ranks them, indexes their names and counts
// the scopes' tested countries.
func (b *builder) providers() error {
	c := newCatalog(b.in)
	entries, err := realProviders(b.in, b.s, c, b.ds.Targets)
	if err != nil {
		return err
	}
	addCountryLatencyRanks(entries)
	addRanks(entries)
	addHeadlinePriceRanks(entries)
	names := map[string]string{}
	slugOfFramework := map[string]string{}
	for _, e := range entries {
		names[e.P.Slug] = e.P.Name
		if e.P.Framework != nil {
			slugOfFramework[e.P.Framework.Provider] = e.P.Slug
		}
	}
	b.entries, b.names, b.slugOfFramework = entries, names, slugOfFramework
	addScopeCountries(b.ds.Scopes, entries, groupsByProvider(b.s))
	addScopePulse(b.ds.Scopes, b.s)
	return nil
}

// siteRecords fills the reviews, community runs and changelog, newest first.
func (b *builder) siteRecords() error {
	in, ds := b.in, b.ds
	ds.Reviews = []model.UserReview{}
	for _, r := range in.Reviews {
		ds.Reviews = append(ds.Reviews, model.UserReview{ID: r.ID, ProviderSlug: r.ProviderSlug, Author: r.Author, Role: r.Role, Rating: Num(r.Rating),
			Title: r.Title, Body: r.Body, Product: r.ProductKind, UseCase: r.UseCase, MonthlySpend: r.Spend, MonthsUsed: Num(r.MonthsUsed),
			Verification: r.Verification, VerificationLabel: verificationLabel(r.Verification), InvoiceMonth: r.InvoiceMonth, LinkedRunID: r.LinkedRunID, HelpfulCount: Num(r.HelpfulCount),
			ProviderReply: r.ProviderReply, AgoSeconds: b.ageOf(r.PostedAt), Source: model.SourceMeasured})
	}
	sortByAge(ds.Reviews, func(r model.UserReview) Num { return r.AgoSeconds })
	ds.CommunityRuns = []model.CommunityRun{}
	for _, r := range in.CommunityRuns {
		run, err := b.communityRun(r)
		if err != nil {
			return err
		}
		ds.CommunityRuns = append(ds.CommunityRuns, run)
	}
	sortByAge(ds.CommunityRuns, func(r model.CommunityRun) Num { return r.AgoSeconds })
	ds.Changelog = []model.ChangeEvent{}
	for _, r := range in.Changelog {
		ch := model.ChangeEvent{ID: r.ID, Kind: r.Kind, ProviderSlug: r.ProviderSlug,
			Delta: ptrNum(r.Delta), Unit: r.Unit, AgoSeconds: b.ageOf(r.OccurredAt), Severity: r.Severity, Source: model.SourceMeasured}
		if r.ProviderSlug != nil {
			ch.ProviderName = model.S(b.nameOrSlug(*r.ProviderSlug))
		}
		ds.Changelog = append(ds.Changelog, ch)
	}
	sortByAge(ds.Changelog, func(r model.ChangeEvent) Num { return r.AgoSeconds })
	return nil
}

func (b *builder) communityRun(r store.CommunityRunRow) (model.CommunityRun, error) {
	run := model.CommunityRun{ID: r.ID, ProviderSlug: r.ProviderSlug, ProviderName: b.nameOrSlug(r.ProviderSlug), SubmittedBy: r.SubmittedBy,
		Region: r.Region, Product: r.ProductKind, PlanTier: r.PlanTier, CollectorVersion: r.CollectorVersion, Status: r.Status,
		RejectionReason: r.RejectionReason, Requests: ptrNum(r.Requests), SuccessRate: ptrNum(r.SuccessRate), LatencyP50: ptrNum(r.LatencyP50),
		UniqueIps: ptrNum(r.UniqueIps), OverlapShare: ptrNum(r.OverlapShare), DeltaSuccessPts: ptrNum(r.DeltaSuccessPts),
		DeltaLatencyPct: ptrNum(r.DeltaLatencyPct), AgoSeconds: b.ageOf(r.SubmittedAt), Source: model.SourceMeasured}
	var err error
	if run.Targets, err = decodeRaw[model.TargetBenchmark](r.Targets, "community run "+r.ID+" targets"); err != nil {
		return run, err
	}
	if run.SuccessSeries, err = decodeRaw[model.SeriesPoint](r.SuccessSeries, "community run "+r.ID+" success_series"); err != nil {
		return run, err
	}
	if run.LatencySeries, err = decodeRaw[model.LatencyPoint](r.LatencySeries, "community run "+r.ID+" latency_series"); err != nil {
		return run, err
	}
	return run, nil
}

// verificationLabel is a review's verification as the site prints it; an
// unknown value as it is.
func verificationLabel(v string) string {
	if label, ok := model.VerificationLabels[v]; ok {
		return label
	}
	return v
}

// tape fills the tape: the measured events and the editorial rows in one
// time order, newest first (measured first at the same time).
func (b *builder) tape() error {
	in, ds, s := b.in, b.ds, b.s
	products := []TapeProduct{}
	if s != nil {
		for _, p := range s.Products {
			// The site's display name (catalog) wins over the snapshot's.
			name := p.Name
			if n, ok := b.names[b.slugOfFramework[p.Provider]]; ok {
				name = n
			}
			products = append(products, TapeProduct{Provider: p.Provider, Name: name, Kind: p.Kind})
		}
	}
	for _, fp := range in.FrameworkProducts {
		products = append(products, TapeProduct{Provider: fp.Provider, Name: b.nameOrSlug(b.slugOfFramework[fp.Provider]), Kind: fp.Name})
	}
	b.res.Events = newEventWindow(in, eventText{products: products, slugs: b.slugOfFramework})
	tapeEvents := b.res.Events.measuredTape(in.TapeHeads)
	tapeRef := ds.GeneratedAt
	if s != nil {
		tapeRef = s.GeneratedAt
	}
	sorted := append([]store.EventJSON{}, tapeEvents...)
	sort.SliceStable(sorted, func(i, j int) bool {
		x, y := snap.Ms(sorted[i].At), snap.Ms(sorted[j].At)
		if x != y {
			return x > y
		}
		return sorted[i].ID > sorted[j].ID
	})
	ds.Tape = TapeFromEvents(sorted, products, tapeRef, b.slugOfFramework)
	for _, r := range in.Tape {
		at := JSISO(r.OccurredAt)
		ds.Tape = append(ds.Tape, model.TapeEvent{ID: r.ID, Kind: r.Kind, Event: r.Kind, ProviderSlug: r.ProviderSlug,
			ProviderName: b.nameOrSlug(r.ProviderSlug), Product: r.Product, Title: r.Title, Detail: r.Detail, Signal: Num(r.Signal),
			Value: r.Value, AgoSeconds: b.ageOf(r.OccurredAt), At: &at, Source: model.SourceEditorial, Values: eventValues("", nil)})
	}
	sort.SliceStable(ds.Tape, func(i, j int) bool { return snap.Ms(model.SV(ds.Tape[i].At)) > snap.Ms(model.SV(ds.Tape[j].At)) })
	return nil
}

// naManifests fills the N/A manifests of the providers, targets, scopes and
// profiles.
func (b *builder) naManifests() error {
	ds := b.ds
	var err error
	for _, e := range b.entries {
		declared := append([]string{}, e.Declared...)
		declared = append(declared, "overlap.likelyUpstream")
		for _, k := range e.P.ProductMeasurements.Keys {
			declared = append(declared, "productMeasurements."+k+".overlap.likelyUpstream")
		}
		e.P.NA, err = naOf(e.P, declared, true, "framework")
		if err != nil {
			return err
		}
		e.P.NA = withoutPublishedEmpties(e)
	}
	for i := range ds.Targets {
		if ds.Targets[i].NA, err = naOf(ds.Targets[i], nil, false); err != nil {
			return err
		}
	}
	for i := range ds.Scopes {
		if err = scopeNA(&ds.Scopes[i]); err != nil {
			return err
		}
	}
	for i := range ds.Profiles {
		if ds.Profiles[i].NA, err = naOf(ds.Profiles[i], nil, false); err != nil {
			return err
		}
	}
	return nil
}

// recordCoverage fills the records' N/A manifests and counts them per record
// kind for the coverage report.
func (b *builder) recordCoverage() error {
	ds := b.ds
	b.records = map[string]CoverageRecords{}
	var err error
	for i := range ds.Reviews {
		if ds.Reviews[i].NA, err = naOf(ds.Reviews[i], nil, false); err != nil {
			return err
		}
		b.countRecords("reviews", ds.Reviews[i].NA)
	}
	for i := range ds.CommunityRuns {
		if ds.CommunityRuns[i].NA, err = naOf(ds.CommunityRuns[i], nil, false); err != nil {
			return err
		}
		b.countRecords("communityRuns", ds.CommunityRuns[i].NA)
	}
	for i := range ds.Changelog {
		if ds.Changelog[i].NA, err = naOf(ds.Changelog[i], nil, false); err != nil {
			return err
		}
		b.countRecords("changelog", ds.Changelog[i].NA)
	}
	for i := range ds.Tape {
		// An event's place, target and values are null by contract where it has none.
		if ds.Tape[i].NA, err = naOf(ds.Tape[i], nil, false, tapeContractNull...); err != nil {
			return err
		}
		b.countRecords("tape", ds.Tape[i].NA)
	}
	for _, name := range []string{"reviews", "communityRuns", "changelog", "tape"} {
		if _, ok := b.records[name]; !ok {
			b.records[name] = CoverageRecords{Fields: map[string]int{}}
		}
	}
	return nil
}

func (b *builder) countRecords(name string, na []string) {
	r := b.records[name]
	if r.Fields == nil {
		r.Fields = map[string]int{}
	}
	r.Count++
	r.NACount += len(na)
	for _, p := range na {
		r.Fields[pattern(p)]++
	}
	b.records[name] = r
}

// datasetNA fills the dataset's own N/A manifest.
func (b *builder) datasetNA() error {
	ds := b.ds
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
	var err error
	if ds.NA, err = naOf(meta, []string{}, false); err != nil {
		return err
	}
	b.res.Dataset = ds
	return nil
}

// details fills every provider's detail: its windows per product and its own
// records.
func (b *builder) details() {
	ds := b.ds
	for _, e := range b.entries {
		p := e.P
		d := &ProviderDetail{Provider: p, Windows: map[string]map[string]*WindowStats{}, Reviews: []model.UserReview{},
			CommunityRuns: []model.CommunityRun{}, Changelog: []model.ChangeEvent{}, Tape: []model.TapeEvent{}, GeneratedAt: ds.GeneratedAt}
		for _, k := range p.ProductMeasurements.Keys {
			view := providerForProduct(p, k)
			d.Windows[k] = map[string]*WindowStats{}
			for _, r := range Ranges {
				d.Windows[k][r.ID] = windowStats(view, e.Snap[k], r.ID, cardTargets(ds.Targets))
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
		b.res.Details[p.Slug] = d
	}
}

// assemble builds the pages, lists, search index and coverage report.
func (b *builder) assemble() error {
	res, ds, s, in := b.res, b.ds, b.s, b.in
	var err error
	if res.Pages, err = buildPages(b.ctx, ds.GeneratedAt, b.entries, b.stats, res.Details, s, in.Snapshot); err != nil {
		return err
	}
	res.Pages.Rankings, res.Pages.RankingsByKind = rankings(ds, b.entries, b.stats, res.Details)
	res.Pages.scoreCharts(b.entries, b.stats, res.Details)
	res.Pages.addRangeRanks()
	res.Pages.addProtocols(b.entries, ds.Scopes)
	res.Pages.addTargetTraffic(b.entries, ds.Targets)
	if err := res.Pages.listNA(b.entries); err != nil {
		return err
	}
	res.Pages.Published = publishedProducts(s, b.entries, in.Providers)
	c := &Context{GeneratedAt: ds.GeneratedAt, Snapshot: s, Entries: b.entries, Stats: b.stats, Details: res.Details,
		Dataset: ds, Events: &res.Events, Inputs: in, Pages: res.Pages}
	res.Pages.addChangelog(c)
	res.Pages.buildRegistered(c)
	res.Events.buildLive(c)
	ds.Pages = datasetPages(res.Pages, &res.Events)
	ds.Lists = datasetLists(res.Pages, s, b.entries)
	var networks []RankingRow
	if r := res.Pages.RankingsByKind[searchRanking]; r != nil {
		networks = r.Networks
	}
	res.Search = buildSearchIndex(networks, ds.Targets, ds.GeneratedAt)
	res.Coverage = coverage(ds, res.BuiltAt, b.records)
	return nil
}
