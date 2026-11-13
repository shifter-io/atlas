package build

import (
	"fmt"
	"net/http"
	"slices"
	"strings"
	"time"

	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/snap"
	"github.com/Zeus-Development/atlas/api/internal/store"
)

// /v1/networks/{slug}/pricing-history?kind= (api/answers/pricing-history.md):
// the list rate in force on each of the last PriceHistoryDays days, from
// site.pricing_history, and its last change (NET-PRICE-HISTORY). Built once
// per build, rendered on first request.
func init() {
	RegisterAnswer(PageAnswer{
		Name:      "pricing-history",
		Route:     "GET /v1/networks/{slug}/pricing-history",
		Key:       pricingHistoryKey,
		Build:     buildPricingHistory,
		Cut:       heldBody,
		InDataset: true,
		Request:   pricingHistoryRequest,
		Samples:   []string{"/v1/networks/all/pricing-history", "/v1/networks/all/pricing-history?kind=residential"},
	})
}

// heldBody is the body built for path: the build makes each kind's small
// answer, and only those asked for are rendered.
func heldBody(held map[string]any, path string) (any, bool) {
	body, ok := held[path]
	return body, ok
}

// PriceHistoryDays is how many days the price history covers, the build's
// day included.
const PriceHistoryDays = 90

// PricePoint is the rate V in force T days before the build.
type PricePoint struct {
	T int `json:"t"`
	V Num `json:"v"`
}

// PricingHistoryAnswer is a network kind's price history: Points oldest
// first, only the days a rate was in force; without history every other
// field is null and listed in NA. KindDefault is as RunsAnswer's.
type PricingHistoryAnswer struct {
	GeneratedAt     string       `json:"generatedAt"`
	Network         string       `json:"network"`
	Kind            string       `json:"kind"`
	KindDefault     bool         `json:"kindDefault"`
	Points          []PricePoint `json:"points"`
	LastChangePct   Num          `json:"lastChangePct"`
	DaysSinceChange Num          `json:"daysSinceChange"`
	NA              []string     `json:"na"`
}

func pricingHistoryKey(r *http.Request) (string, error) {
	slug, err := PathSlug(r)
	if err != nil {
		return "", err
	}
	q, err := Query(r, "kind")
	if err != nil {
		return "", err
	}
	kind := q.Get("kind")
	switch {
	case kind == "":
		kind = "primary"
	case !slices.Contains(model.ProxyKinds, kind):
		return "", InvalidValue("kind", kind, model.ProxyKinds...)
	}
	return "/v1/pricing-history/" + slug + "/" + kind, nil
}

// pricingHistoryRequest is the request for a stored path: the "primary"
// body is the one without kind.
func pricingHistoryRequest(path string) string {
	slug, kind, _ := strings.Cut(strings.TrimPrefix(path, "/v1/pricing-history/"), "/")
	base := "/v1/networks/" + slug + "/pricing-history"
	if kind == "primary" {
		return base
	}
	return base + "?kind=" + kind
}

func buildPricingHistory(c *Context) (map[string]any, error) {
	generated, ok := snap.ParseTime(c.GeneratedAt)
	if !ok {
		return nil, fmt.Errorf("generatedAt %q is not a time", c.GeneratedAt)
	}
	today := utcDay(generated)
	rows := map[string][]store.PricingHistoryRow{}
	for _, r := range c.Inputs.PricingHistory {
		rows[r.ProviderSlug+"/"+r.Category] = append(rows[r.ProviderSlug+"/"+r.Category], r)
	}
	out := map[string]any{}
	add := func(slug, segment, kind string, steps []priceStep) {
		ans := priceHistory(c.GeneratedAt, slug, kind, today, steps)
		ans.KindDefault = segment == "primary"
		out["/v1/pricing-history/"+slug+"/"+segment] = ans
	}
	for _, e := range c.Entries {
		for _, kind := range append(slices.Clone(model.ProxyKinds), "primary") {
			k := kind
			if kind == "primary" {
				k = e.P.PrimaryKind
			}
			steps := priceSteps(rows[e.P.Slug+"/"+k], e.Snap[k], today)
			add(e.P.Slug, kind, k, steps)
		}
	}
	// "all" has no list rate of its own: its history is empty.
	if _, ok := c.Details[AllNetworks]; !ok {
		for _, kind := range append(slices.Clone(model.ProxyKinds), "primary") {
			named := kind
			if kind == "primary" {
				named = "any"
			}
			add(AllNetworks, kind, named, nil)
		}
	}
	return out, nil
}

// priceStep is a list rate in force from a day on.
type priceStep struct {
	from time.Time
	rate float64
}

func utcDay(t time.Time) time.Time { return t.UTC().Truncate(24 * time.Hour) }

// priceSteps is the history's rates in force by today, oldest first, and the
// snapshot's pricing after them when it took effect later than the newest
// row. A kind without history has no steps.
func priceSteps(rows []store.PricingHistoryRow, p *snap.Product, today time.Time) (steps []priceStep) {
	for _, r := range rows {
		rate := displayRate(model.Pricing{PricePerGb: ptrNum(r.PricePerGb), PricePerGbAtScale: ptrNum(r.PricePerGbAtScale)})
		if from := utcDay(r.EffectiveFrom); rate.OK() && !from.After(today) {
			steps = append(steps, priceStep{from, rate.F()})
		}
	}
	if len(steps) == 0 || p == nil || p.Pricing == nil || p.Pricing.EffectiveFrom == nil {
		return steps
	}
	at, ok := snap.ParseTime(*p.Pricing.EffectiveFrom)
	rate := displayRate(model.Pricing{PricePerGb: p.Pricing.EntryPerGb, PricePerGbAtScale: p.Pricing.LowestPerGb})
	if from := utcDay(at); ok && rate.OK() && from.After(steps[len(steps)-1].from) && !from.After(today) {
		return append(steps, priceStep{from, rate.F()})
	}
	return steps
}

// priceHistory is the answer for steps: the rate in force each day and the
// newest change between two steps.
func priceHistory(generatedAt, slug, kind string, today time.Time, steps []priceStep) *PricingHistoryAnswer {
	ans := &PricingHistoryAnswer{GeneratedAt: generatedAt, Network: slug, Kind: kind, Points: []PricePoint{},
		LastChangePct: nan, DaysSinceChange: nan, NA: []string{}}
	for t := PriceHistoryDays - 1; t >= 0; t-- {
		on := today.AddDate(0, 0, -t)
		i := len(steps) - 1
		for i >= 0 && steps[i].from.After(on) {
			i--
		}
		if i >= 0 {
			ans.Points = append(ans.Points, PricePoint{T: t, V: Num(steps[i].rate)})
		}
	}
	for i := len(steps) - 1; i > 0; i-- {
		if prev := steps[i-1].rate; steps[i].rate != prev {
			ans.LastChangePct = jsRound2((steps[i].rate - prev) / prev * 100)
			ans.DaysSinceChange = Num(today.Sub(steps[i].from).Hours() / 24)
			break
		}
	}
	for _, f := range []struct {
		name string
		null bool
	}{{"lastChangePct", !ans.LastChangePct.OK()}, {"daysSinceChange", !ans.DaysSinceChange.OK()}} {
		if f.null {
			ans.NA = append(ans.NA, f.name)
		}
	}
	return ans
}
