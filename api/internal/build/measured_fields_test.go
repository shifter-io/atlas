package build

import (
	"context"
	"encoding/json"
	"slices"
	"strings"
	"testing"

	"github.com/shifter-io/atlas/api/internal/model"
	"github.com/shifter-io/atlas/api/internal/snap"
	"github.com/shifter-io/atlas/api/internal/store"
)

// MEASURED-FIELDS-1: the average payload is the 30d window's wire bytes per
// request (the bytes the wire cost reads); a catalog value wins, and a window
// without wire bytes leaves it null.
func TestAvgPayloadKbIsTheWindowsWireBytesPerRequest(t *testing.T) {
	t.Parallel()
	price, kb := 2.0, 7.5
	w30 := snap.Metrics{Completed: 1000, BytesUp: 48_000, BytesDown: 2_000_000, RequestSuccessRate: 0.5}
	for _, c := range []struct {
		name    string
		row     *store.PricingRow
		metrics snap.Metrics
		want    Num
	}{
		{"derived", &store.PricingRow{ProviderSlug: "a", Category: "residential", PricePerGb: &price}, w30, 2},
		{"catalog", &store.PricingRow{ProviderSlug: "a", Category: "residential", PricePerGb: &price, AvgPayloadKb: &kb}, w30, 7.5},
		{"no wire bytes", &store.PricingRow{ProviderSlug: "a", Category: "residential", PricePerGb: &price}, snap.Metrics{Completed: 1000}, nan},
	} {
		c.metrics.SuccessRate = c.metrics.RequestSuccessRate
		cat := &catalog{pricing: map[[2]string]*store.PricingRow{{"a", "residential"}: c.row}, trust: map[[2]string]*store.TrustRow{}}
		m := model.EmptyMeasured()
		mergeCatalog("a", "residential", &m, cat, &c.metrics)
		if got := m.Pricing.AvgPayloadKb; got.OK() != c.want.OK() || got.OK() && got != c.want {
			t.Errorf("%s: avgPayloadKb %v, want %v", c.name, got, c.want)
		}
	}
	m := model.EmptyMeasured()
	mergeCatalog("a", "residential", &m, &catalog{pricing: map[[2]string]*store.PricingRow{}, trust: map[[2]string]*store.TrustRow{}}, &w30)
	// The same wire bytes: 1,000 requests of 2 KB at $2/GB, half of them successful.
	gb := m.Pricing.AvgPayloadKb.F() * 1024 * 1000 / gbBytes
	if m.Pricing.AvgPayloadKb != 2 || !near(wireCostPer1k(2, w30), gb*2/0.5) {
		t.Errorf("avgPayloadKb %v and wire cost %v disagree", m.Pricing.AvgPayloadKb, wireCostPer1k(2, w30))
	}
}

// pricedEntry is a network measured in kinds, each at its list rate (NaN:
// unpriced), its headline kind the first.
func pricedEntry(slug string, rates map[string]Num, kinds ...string) *entry {
	p := &model.Provider{Slug: slug, PrimaryKind: kinds[0], ProductMeasurements: model.NewMeasurementMap(), Measured: model.EmptyMeasured()}
	for _, k := range kinds {
		pm := &model.ProductMeasurement{Kind: k, Measured: model.EmptyMeasured()}
		pm.Pricing.PricePerGb = rates[k]
		p.ProductMeasurements.Set(k, pm)
	}
	p.Measured = p.ProductMeasurements.Get(kinds[0]).Measured
	return &entry{P: p}
}

// MEASURED-FIELDS-1: the headline price rank is the list rate's competition
// rank among the networks of its kind, cheapest first, ties sharing the
// better rank; a catalog rank wins, a network without a list rate has none.
func TestHeadlinePriceRankRanksTheListRatesOfAKind(t *testing.T) {
	t.Parallel()
	res, dc := "residential", "datacenter"
	entries := []*entry{
		pricedEntry("a", map[string]Num{res: 3}, res),
		pricedEntry("b", map[string]Num{res: 2, dc: 9}, res, dc),
		pricedEntry("c", map[string]Num{res: 3}, res),
		pricedEntry("d", map[string]Num{res: nan}, res),
		pricedEntry("e", map[string]Num{res: 1}, res),
	}
	entries[4].P.ProductMeasurements.Get(res).Pricing.HeadlinePriceRank = 7 // a catalog rank
	catalogOnly := &entry{P: &model.Provider{Slug: "f", PrimaryKind: dc, Measured: model.EmptyMeasured()}}
	catalogOnly.P.Measured.Pricing.PricePerGbAtScale = 4
	entries = append(entries, catalogOnly)
	addHeadlinePriceRanks(entries)
	for _, c := range []struct {
		e    *entry
		kind string
		want Num
	}{{entries[0], res, 3}, {entries[1], res, 2}, {entries[1], dc, 2}, {entries[2], res, 3}, {entries[3], res, nan}, {entries[4], res, 7}} {
		got := c.e.P.ProductMeasurements.Get(c.kind).Pricing.HeadlinePriceRank
		if got.OK() != c.want.OK() || got.OK() && got != c.want {
			t.Errorf("%s %s: rank %v, want %v", c.e.P.Slug, c.kind, got, c.want)
		}
	}
	if entries[1].P.Measured.Pricing.HeadlinePriceRank != 2 || catalogOnly.P.Measured.Pricing.HeadlinePriceRank != 1 {
		t.Errorf("headline blocks: b %v, f %v", entries[1].P.Measured.Pricing.HeadlinePriceRank, catalogOnly.P.Measured.Pricing.HeadlinePriceRank)
	}
}

// MEASURED-FIELDS-1: a country row of a range with requests that has none of
// its own is not measured per place; a range without requests has no run.
func TestANetworksCountryRowsNullsAreNotMeasuredPerPlace(t *testing.T) {
	t.Parallel()
	a := &NetworkAnswer{Ranges: map[string]*RangeTotals{"24h": {Requests: 10}, "7d": {Requests: 0}}}
	code := a.windowCode(naFacts{measured: true, priced: true}, totalsRange)
	for path, want := range map[string]string{
		"ranges.24h.countries[].successRate": NANotMeasuredPerPlace,
		"ranges.24h.countries[].latencyP50":  NANotMeasuredPerPlace,
		"ranges.7d.countries[].latencyP50":   NANoRun,
		"ranges.24h.latencyP90":              NANotMeasured,
	} {
		if got := code(path); got != want {
			t.Errorf("%s: %s, want %s", path, got, want)
		}
	}
}

// MEASURED-FIELDS-1: a place page carries its rows' requests, success rate
// and p50 where a test records them per place (a pulse's city row), and
// lists a row's nulls (a daily test's city) with not_measured_per_place.
func TestAPlacePageListsItsRowsNullsPerPlace(t *testing.T) {
	t.Parallel()
	rows := []store.NetRow{
		{Place: &store.Place{CountryCode: ptr("DE"), RegionCode: ptr("BE"), Region: ptr("Berlin"), City: ptr("Berlin")}, Ips: 9, Completed: 8, Successes: 6, P50: 150},
		{Place: &store.Place{CountryCode: ptr("US"), RegionCode: ptr("MA"), Region: ptr("Massachusetts"), City: ptr("Boston")}, Ips: 4, Completed: 0, Successes: 0, P50: -1},
	}
	l, err := store.NewNetList(map[string][]store.NetRow{"city": rows})
	if err != nil {
		t.Fatal(err)
	}
	src := []placeSource{{list: l, scale: nan, network: "a", name: "A", kind: "residential"}}
	merged, err := mergeSources(context.Background(), src, "city", "", false)
	if err != nil {
		t.Fatal(err)
	}
	pg := &Pages{Network: map[string]*NetworkAnswer{"a/residential": {}},
		lists: map[listKey]*placeIndex{{"a/residential", "30d", "city"}: {srcs: src, dimension: "city", total: len(merged), first: merged}}}
	page, ok, err := pg.List(context.Background(), "at", "a/residential", "30d", "city", ListFilter{}, 1)
	if err != nil || !ok || len(page.Rows) != 2 {
		t.Fatalf("page %+v ok %v err %v", page, ok, err)
	}
	berlin, boston := page.Rows[0], page.Rows[1]
	if berlin.Requests != 8 || berlin.SuccessRate != 0.75 || berlin.LatencyP50 != 150 {
		t.Errorf("pulse city row %+v", berlin)
	}
	if boston.Requests != 0 || boston.SuccessRate.OK() || boston.LatencyP50.OK() {
		t.Errorf("daily city row %+v", boston)
	}
	if got := page.ReasonOf("rows[].latencyP50"); got != NANotMeasuredPerPlace {
		t.Errorf("rows[].latencyP50: %q, want %s (na %v)", got, NANotMeasuredPerPlace, page.NA)
	}
	// The row's success rate is internal (SUCCESS-RATE-REMOVE-1, #900).
	if slices.Contains(page.NA, "rows[].successRate") {
		t.Errorf("na %v lists the internal success rate", page.NA)
	}
	if second, _, _ := pg.List(context.Background(), "at", "a/residential", "30d", "city", ListFilter{Query: "boston"}, 1); second.ReasonOf("rows[].latencyP50") != NANotMeasuredPerPlace {
		t.Errorf("searched page na %v", second.NA)
	}
}

// MEASURED-FIELDS-1 (amended): a network answer lists its plans' empty
// amounts and tiers and its changelog entries' kind-bound fields, each with a
// specific reason: a plan named by its index (PRICING-CODES-1),
// not_applicable where the field has no meaning for the row, catalog where
// the catalog lacks it.
func TestANetworkAnswerGivesItsPlansAndChangelogAReason(t *testing.T) {
	t.Parallel()
	tierless := json.RawMessage(`{"tier":null}`)
	a := &NetworkAnswer{Kind: "residential", Ranges: map[string]*RangeTotals{}, Changelog: []ChangeEntry{
		{ID: "o", Kind: "incident", Incident: &ChangeOutage{DurationMinutes: nan}, Network: &ChangeNetwork{}},
		{ID: "p", Kind: "price", Event: "price_cut", Values: ChangeValues{"plan": nil}},
	}, Profile: &NetworkProfile{Founded: nan}}
	a.Profile.Pricing.Plans = []model.PricingPlan{
		{Name: model.S("Pay as you go"), Price: 3, Gb: nan, Ips: nan, Catalog: tierless},
		{Name: model.S("Static 10"), Price: 20, Gb: nan, Ips: 10},
		{Name: model.S("100 GB"), Price: 300, Gb: 100, Ips: nan},
	}
	if err := a.listNA(nil); err != nil {
		t.Fatal(err)
	}
	for _, c := range []struct {
		of         *NAList
		path, want string
	}{
		{&a.Profile.NAList, "pricing.plans[0].gb", NACatalog},
		{&a.Profile.NAList, "pricing.plans[0].ips", NANotApplicable},
		{&a.Profile.NAList, "pricing.plans[0].catalog.tier", NANotApplicable},
		{&a.Profile.NAList, "pricing.plans[0].audience", NACatalog},
		{&a.Profile.NAList, "pricing.plans[1].gb", NANotApplicable},
		{&a.Profile.NAList, "pricing.plans[2].ips", NANotApplicable},
		{&a.Profile.NAList, "founded", NACatalog},
		{&a.NAList, "changelog[].network", NANotApplicable},
		{&a.NAList, "changelog[].incident.durationMinutes", NANotApplicable},
		{&a.NAList, "changelog[].incident", NANotApplicable},
		{&a.NAList, "changelog[].values.plan", NANotPublished},
	} {
		if got := c.of.ReasonOf(c.path); got != c.want {
			t.Errorf("%s: %q, want %s (na %v)", c.path, got, c.want, c.of.NA)
		}
	}
	for _, l := range []*NAList{&a.NAList, &a.Profile.NAList} {
		for _, p := range l.NA {
			if l.ReasonOf(p) == "" {
				t.Errorf("%s has no reason code", p)
			}
		}
	}
}

// PRICING-CODES-1: a null trial number's reason: not_applicable without a
// trial, for a credit's GB and for the days of a trial in numbers (no time
// limit); catalog where the catalog lacks it (a trial text without numbers).
func TestTrialNumbersHaveAReason(t *testing.T) {
	t.Parallel()
	price, none := model.Pricing{Trial: model.S("$1.97 trial, 150 MB, no expiry"), TrialPrice: 1.97, TrialDays: nan}, model.S("None")
	for _, c := range []struct {
		p           model.Pricing
		field, want string
	}{
		{price, "trialDays", NANotApplicable},
		{price, "trialNeedsCard", NACatalog},
		{model.Pricing{Trial: model.S("$5 welcome credit"), TrialPrice: 0, TrialGb: nan}, "trialGb", NANotApplicable},
		{model.Pricing{Trial: model.S("$5 welcome credit"), TrialPrice: 0, TrialGb: nan}, "trialNeedsCard", NACatalog},
		{model.Pricing{Trial: none, TrialPrice: nan}, "trialPrice", NANotApplicable},
		{model.Pricing{Trial: none, TrialPrice: nan}, "trialDays", NANotApplicable},
		{model.Pricing{Trial: model.S("Available"), TrialPrice: nan}, "trialDays", NACatalog},
		{model.Pricing{Trial: model.S("Available"), TrialPrice: nan}, "trialPrice", NACatalog},
		{model.Pricing{TrialPrice: nan}, "trialGb", NACatalog},
	} {
		if got := trialCodes(c.p)["pricing."+c.field]; got != c.want {
			t.Errorf("%s of %q: %q, want %s", c.field, model.SV(c.p.Trial), got, c.want)
		}
	}
}

// PLAN-NULL-REASON-1 (#1060): a plan the provider gives no name, or names no
// pool, is listed in the network answer's na and the dataset file's with
// not_named_by_provider; a named plan and pool are not. The walker test
// (naProblems) no longer exempts the two nulls by contract.
func TestAnUnnamedPlanAndPoolAreListedWithTheirReason(t *testing.T) {
	t.Parallel()
	plans := []model.PricingPlan{
		{Price: 3, Gb: 1, Ips: nan, Audience: model.S("all"), Catalog: json.RawMessage(`{"pool":null,"tier":"t1"}`)},
		{Name: model.S("Starter"), Price: 20, Gb: 10, Ips: nan, Audience: model.S("all"), Catalog: json.RawMessage(`{"pool":"Full Geo","tier":"t1"}`)},
	}
	if byContract("pricing.plans[].name") || byContract("pricing.plans[].catalog.pool") {
		t.Error("a null plan name or pool is still null by contract")
	}
	a := &NetworkAnswer{Kind: "residential", Ranges: map[string]*RangeTotals{}, Profile: &NetworkProfile{Founded: nan}}
	a.Profile.Pricing.Plans = plans
	if err := a.listNA(nil); err != nil {
		t.Fatal(err)
	}
	for _, p := range []string{"pricing.plans[0].name", "pricing.plans[0].catalog.pool"} {
		if got := a.Profile.ReasonOf(p); got != NANotNamedByProvider {
			t.Errorf("%s: %q, want %s (na %v)", p, got, NANotNamedByProvider, a.Profile.NA)
		}
	}
	for _, p := range []string{"pricing.plans[1].name", "pricing.plans[1].catalog.pool"} {
		if slices.Contains(a.Profile.NA, p) {
			t.Errorf("the named plan lists %s", p)
		}
	}
	for _, p := range naProblems(t, a, nil) {
		if strings.Contains(p, "plans") {
			t.Error(p)
		}
	}
	dataset, err := naOf(struct {
		Pricing model.Pricing `json:"pricing"`
	}{model.Pricing{Plans: plans}}, nil, false)
	if err != nil {
		t.Fatal(err)
	}
	for _, p := range []string{"pricing.plans[].name", "pricing.plans[].catalog.pool"} {
		if !slices.Contains(dataset, p) {
			t.Errorf("dataset na %v lacks %s", dataset, p)
		}
		if got := reasonFor(p); got.code != NANotNamedByProvider || got.source != "site.pricing_plans" {
			t.Errorf("%s: coverage reason %v", p, got)
		}
	}
}
