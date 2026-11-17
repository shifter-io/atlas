package build

import (
	"encoding/json"
	"fmt"
	"testing"
	"time"

	"github.com/shifter-io/atlas/api/internal/model"
	"github.com/shifter-io/atlas/api/internal/store"
	"github.com/shifter-io/atlas/api/internal/testscale"
)

// networkScale is the real shape of a /v1/networks/{slug} answer
// (NETWORK-PAGE-SIZE-1, #938), each count production's largest on
// 2026-11-15 (site version 32) or the issue's, whichever is more: 18
// networks, 230 requested countries each with its status, 249 exit
// countries (each range lists its top 50), the week's 14 targets on two
// engines (production has 12), 17 overlap peers, 35 priced plans with their
// catalog (Geonode's), 40 tape rows, 20 changelog rows and 30 incidents.
var networkScale = struct {
	Networks, Requested, Plans, Tape, Changelog, Incidents int
}{Networks: 18, Requested: 230, Plans: 35, Tape: 40, Changelog: 20, Incidents: 30}

// networkScaleShape is the week's snapshot shape networkScaleInputs starts
// from: success scores and each network's own series gaps, as served.
var networkScaleShape = testscale.Shape{Networks: networkScale.Networks, Countries: 1, Exits: 249, Tail: 30, Browser: true,
	SuccessScores: true, Gaps: true}

// networkPlanCatalog is a plan's catalog as site.pricing_plans holds it, at
// production's string lengths (Geonode's first plan on 2026-11-15).
const networkPlanCatalog = `{"billing":"monthly","checkedAt":"2026-11-12","discountSource":null,"originalPrice":7.92,` +
	`"originalUnitRate":0.792,"pool":"Residential rotating","poolKinds":["residential"],` +
	`"source":"https://geonode.com/pricing/residential-proxies","unitRate":0.792,` +
	`"url":"https://geonode.com/pricing/residential-proxies","audience":"all"}`

// networkScaleInputs is the week's build (testscale.WeekInputs) published
// as payload v4 (testscale.NetworkHead, its list rows at productionRows'
// widths) with networkScale's requested countries and incidents in
// every network's summary (withSummaries), and the site rows (pricing,
// plans, changelog) and tape heads of networkScale for every network.
func networkScaleInputs(t *testing.T) *store.Inputs {
	t.Helper()
	in, err := testscale.WeekInputs(networkScaleShape)
	if err != nil {
		t.Fatal(err)
	}
	lists := map[int64]*store.NetList{}
	head, err := testscale.NetworkHead(in.Snapshot, networkScale.Requested, func(_ testscale.NetWindow, rows map[string][]store.NetRow) (int64, map[string]int, error) {
		id := int64(len(lists) + 1)
		l, err := store.NewNetList(productionRows(rows))
		lists[id] = l
		return id, testscale.RowCounts(rows), err
	})
	if err != nil {
		t.Fatal(err)
	}
	asOf, head, err := withSummaries(head)
	if err != nil {
		t.Fatal(err)
	}
	in.Snapshot = &store.SnapshotRow{BuildID: in.Snapshot.BuildID, ContentHash: in.Snapshot.ContentHash, Payload: head}
	if err := in.Snapshot.SetNetworks(lists); err != nil {
		t.Fatal(err)
	}
	for n := range networkScale.Networks {
		slug := fmt.Sprintf("net%02d", n)
		in.Pricing = append(in.Pricing, networkScalePricing(slug))
		in.Plans = append(in.Plans, networkScalePlans(slug)...)
		in.Changelog = append(in.Changelog, networkScaleChangelog(slug, asOf)...)
		in.TapeHeads = append(in.TapeHeads, networkScaleTape(n)...)
	}
	return in
}

// productionRows gives a network list's rows production's widths: counts
// of 6 digits and an IP rank among 17 peers (calculated v15).
func productionRows(rows map[string][]store.NetRow) map[string][]store.NetRow {
	for _, list := range rows {
		for i := range list {
			r := &list[i]
			r.Ips, r.Completed, r.Successes = r.Ips*433, r.Completed*433, r.Successes*433
			r.P50, r.IpsRank, r.IpsPeers = r.P50+354, int32(1+i%17), 17
		}
	}
	return rows
}

// withSummaries gives every network of a v4 head its requested countries'
// statuses (country_status, as the methodology's country_verdicts write
// them), networkScale.Incidents outages in the 30 days before the head's
// as_of, every other network as an overlap peer in each window and a
// success score over every target on each day, and returns that as_of.
func withSummaries(head []byte) (time.Time, []byte, error) {
	var doc map[string]any
	if err := json.Unmarshal(head, &doc); err != nil {
		return time.Time{}, nil, err
	}
	asOf, err := time.Parse(time.RFC3339, doc["as_of"].(string))
	if err != nil {
		return time.Time{}, nil, err
	}
	networks, scores := doc["networks"].([]any), map[string]any{}
	for i, raw := range doc["targets"].([]any) {
		scores[raw.(map[string]any)["id"].(string)] = 0.5 + float64(i)/37
	}
	for j, raw := range networks {
		g := raw.(map[string]any)
		criteria := g["criteria"].(map[string]any)
		requested, status := criteria["requested_countries"].([]any), []any{}
		for i, cc := range requested {
			status = append(status, map[string]any{"country": cc, "status": "counts", "unique_ips": 10000 + 731*i})
		}
		criteria["country_status"] = status
		g["outages"], g["days"] = scaleOutages(requested, asOf), scaleDays(scores, asOf)
		for _, w := range g["windows"].(map[string]any) {
			overlap := w.(map[string]any)["overlap"].(map[string]any)
			overlap["exclusive_share"], overlap["shared_share"], overlap["peers"] = 0.615042558876293, 0.384957441123707, scalePeers(networks, j)
		}
	}
	out, err := json.Marshal(doc)
	return asOf, out, err
}

// scaleOutages is networkScale.Incidents outages of 95 minutes, one per
// requested country, a day apart before asOf.
func scaleOutages(requested []any, asOf time.Time) []any {
	out := []any{}
	for i := range networkScale.Incidents {
		start := asOf.Add(-time.Duration(i*23+1) * time.Hour)
		out = append(out, map[string]any{"country": requested[i], "region": "fra1", "source": "pulse",
			"started_at": start.Format(time.RFC3339), "ended_at": start.Add(95 * time.Minute).Format(time.RFC3339), "duration_s": 5700})
	}
	return out
}

// scaleDays is a network's days (DAY-SCORE-1) over the 32 days to asOf,
// so every day of its 30d window has one, each scored over every target.
func scaleDays(scores map[string]any, asOf time.Time) []any {
	out := []any{}
	for i := range 32 {
		start := asOf.Truncate(24*time.Hour).AddDate(0, 0, i-31)
		out = append(out, map[string]any{"start": start.Format(time.RFC3339), "success_overall": map[string]any{
			"score": 6.294117647058823, "rank": 9, "ranked_of": networkScale.Networks, "status": "ranked", "targets": scores}})
	}
	return out
}

// scalePeers is every network of networks but the j-th as its overlap
// peers, most shared IPs first.
func scalePeers(networks []any, j int) []any {
	out := []any{}
	for i, raw := range networks {
		if i == j {
			continue
		}
		g := raw.(map[string]any)
		out = append(out, map[string]any{"provider": g["provider"], "product_id": g["product_id"], "shared_ips": 78294 - 1000*len(out),
			"ips_ours": 524000, "ips_theirs": 1658273 + 7919*i})
	}
	return out
}

// networkScalePricing is a network's site.pricing row, every figure set.
func networkScalePricing(slug string) store.PricingRow {
	f := func(v float64) *float64 { return &v }
	days := int32(7)
	return store.PricingRow{ProviderSlug: slug, Category: "residential", Model: model.S("payg"), PricePerGb: f(4.75),
		PricePerGbAtScale: f(2.25), MinCommit: f(50), TrialPrice: f(1.99), TrialCredit: f(5), TrialGb: f(1), TrialDays: &days,
		TrialNeedsCard: model.B(true)}
}

// networkScalePlans is a network's networkScale.Plans plans, each with
// networkPlanCatalog and a name at production's length.
func networkScalePlans(slug string) []store.PlanRow {
	out := []store.PlanRow{}
	for i := range networkScale.Plans {
		name := fmt.Sprintf("%d GB Plan (subscription)", 10*(i+1))
		price, gb, discount, rec := 7.92*float64(i+1), 10*float64(i+1), 12.5, i == 3
		out = append(out, store.PlanRow{Catalog: json.RawMessage(networkPlanCatalog), ProviderSlug: slug, Category: "residential",
			Position: int32(i), Name: &name, Price: &price, Gb: &gb, Recommended: &rec, DiscountPct: &discount})
	}
	return out
}

// networkScaleChangelog is a network's networkScale.Changelog catalog
// changelog rows, an hour apart before asOf.
func networkScaleChangelog(slug string, asOf time.Time) []store.ChangeRow {
	out := []store.ChangeRow{}
	for i := range networkScale.Changelog {
		delta := 12.5
		out = append(out, store.ChangeRow{ID: fmt.Sprintf("chg-pricing-%s-%02d", slug, i), Kind: "pricing", ProviderSlug: model.S(slug),
			Headline: "{name} cut its price", Detail: "Entry plan price per GB", Delta: &delta, Unit: "pct", Severity: "good",
			OccurredAt: asOf.Add(-time.Duration(i+1) * time.Hour)})
	}
	return out
}

// networkScaleTape is network n's networkScale.Tape tape heads: a full
// tape (testscale.TapeEvents) of its own, with ids no other network's
// tape has.
func networkScaleTape(n int) []store.Event {
	tape := testscale.TapeEvents(1)[:networkScale.Tape]
	provider := fmt.Sprintf("net%02d", n)
	for i := range tape {
		tape[i].ID -= int64(n * 1000)
		tape[i].Provider = &provider
	}
	return tape
}
