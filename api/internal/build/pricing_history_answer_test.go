package build

import (
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/snap"
	"github.com/Zeus-Development/atlas/api/internal/store"
)

// net00's residential rate went from 2.00 to 1.50 ten days ago: 90 points,
// a -25% change ten days ago; a row not yet in force is left out. A kind
// without rows is empty.
func TestAPriceChangeTenDaysAgo(t *testing.T) {
	t.Parallel()
	in := pagedInputs(t)
	before, err := Build(in)
	if err != nil {
		t.Fatal(err)
	}
	generated, _ := snap.ParseTime(before.Dataset.GeneratedAt)
	today := utcDay(generated)
	daysAgo := func(n int) time.Time { return today.AddDate(0, 0, -n) }
	in.PricingHistory = []store.PricingHistoryRow{
		{ProviderSlug: "net00", Category: "residential", EffectiveFrom: daysAgo(200), PricePerGb: fp(4)},
		{ProviderSlug: "net00", Category: "residential", EffectiveFrom: daysAgo(40), PricePerGb: fp(3), PricePerGbAtScale: fp(2)},
		{ProviderSlug: "net00", Category: "residential", EffectiveFrom: daysAgo(10), PricePerGb: fp(3), PricePerGbAtScale: fp(1.5)},
		{ProviderSlug: "net00", Category: "residential", EffectiveFrom: daysAgo(-5), PricePerGb: fp(1)},
		{ProviderSlug: "net01", Category: "datacenter", EffectiveFrom: daysAgo(5), PricePerGb: fp(1)},
	}
	res, err := Build(in)
	if err != nil {
		t.Fatal(err)
	}
	get := func(target string) *PricingHistoryAnswer {
		return cut[PricingHistoryAnswer](t, res, "pricing-history", target)
	}
	ans := get("/v1/networks/net00/pricing-history")
	if ans == nil || ans.Kind != "residential" || !ans.KindDefault || len(ans.Points) != PriceHistoryDays || ans.LastChangePct != -25 || ans.DaysSinceChange != 10 ||
		len(ans.NA) != 0 {
		t.Fatalf("net00: %+v", ans)
	}
	rates := map[int]Num{}
	for _, p := range ans.Points {
		rates[p.T] = p.V
	}
	if ans.Points[0].T != 89 || rates[89] != 4 || rates[41] != 4 || rates[40] != 2 || rates[11] != 2 || rates[10] != 1.5 || rates[0] != 1.5 {
		t.Errorf("points %v", ans.Points)
	}
	if same := get("/v1/networks/net00/pricing-history?kind=residential"); same == nil || same.KindDefault || len(same.Points) != len(ans.Points) {
		t.Errorf("?kind=residential: %+v", same)
	}
	dc := get("/v1/networks/net01/pricing-history?kind=datacenter")
	if dc == nil || len(dc.Points) != 6 || dc.Points[0].T != 5 || dc.LastChangePct.OK() || strings.Join(dc.NA, " ") != "lastChangePct daysSinceChange" {
		t.Errorf("one price: %+v", dc)
	}
	for _, target := range []string{"/v1/networks/net01/pricing-history", "/v1/networks/all/pricing-history?kind=mobile"} {
		empty := get(target)
		if empty == nil || empty.Points == nil || len(empty.Points) != 0 || empty.DaysSinceChange.OK() ||
			strings.Join(empty.NA, " ") != "lastChangePct daysSinceChange" {
			t.Errorf("%s: %+v", target, empty)
		}
	}
	if all := get("/v1/networks/all/pricing-history"); all == nil || all.Kind != "any" {
		t.Errorf("all: %+v", all)
	}
}

// The snapshot's pricing is the newest step when it took effect after the
// newest row, and only then.
func TestTheSnapshotPriceIsTheCurrentPointWhenNewer(t *testing.T) {
	t.Parallel()
	today := time.Date(2026, 11, 8, 0, 0, 0, 0, time.UTC)
	rows := []store.PricingHistoryRow{{EffectiveFrom: today.AddDate(0, 0, -30), PricePerGbAtScale: fp(2)}}
	product := func(from string) *snap.Product {
		return &snap.Product{Pricing: &snap.Pricing{EffectiveFrom: model.S(from), LowestPerGb: 1.6, EntryPerGb: 3}}
	}
	for _, c := range []struct {
		name    string
		rows    []store.PricingHistoryRow
		product *snap.Product
		want    string
	}{
		{"newer", rows, product("2026-11-01T00:00:00+00:00"), "2 1.6"},
		{"older", rows, product("2026-09-01T00:00:00+00:00"), "2"},
		{"in the future", rows, product("2026-12-01T00:00:00+00:00"), "2"},
		{"unparsed", rows, product("soon"), "2"},
		{"no rows", nil, product("2026-11-01T00:00:00+00:00"), ""},
		{"no pricing", rows, &snap.Product{}, "2"},
	} {
		got := []string{}
		for _, s := range priceSteps(c.rows, c.product, today) {
			got = append(got, fmt.Sprint(s.rate))
		}
		if strings.Join(got, " ") != c.want {
			t.Errorf("%s: %v, want %s", c.name, got, c.want)
		}
	}
	ans := priceHistory("now", "net00", "residential", today, []priceStep{{today.AddDate(0, 0, -30), 2}, {today.AddDate(0, 0, -7), 1.6}})
	if ans.DaysSinceChange != 7 || ans.LastChangePct != -20 {
		t.Errorf("with the snapshot step: %+v", ans)
	}
}

func TestThePricingHistoryKeyRefusesBadQueries(t *testing.T) {
	t.Parallel()
	for target, want := range map[string]string{"/v1/networks/net00/pricing-history": "/v1/pricing-history/net00/primary",
		"/v1/networks/net00/pricing-history?kind=isp": "/v1/pricing-history/net00/isp"} {
		if got, err := storedPath(pricingHistoryKey, target); got != want || err != nil {
			t.Errorf("%s: %q %v", target, got, err)
		}
	}
	badRequest(t, pricingHistoryKey, map[string]string{"/v1/networks/net00/pricing-history?kind=any": " name:kind value:",
		"/v1/networks/net00/pricing-history?kind=Bad!": " name:kind value:", "/v1/networks/net00/pricing-history?page=1": "unknown_parameter map[name:",
		"/v1/networks/-/pricing-history": "invalid_parameter map[format:slug name:"})
}
