package build

import (
	"encoding/json"
	"fmt"
	"sort"
	"strings"

	"github.com/Zeus-Development/proxy-index/api/internal/jsnum"
	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/Zeus-Development/proxy-index/api/internal/snap"
	"github.com/Zeus-Development/proxy-index/api/internal/store"
)

// entry is a built provider plus what derived views need.
type entry struct {
	P *model.Provider
	// Snapshot products per category (measured providers), headline first.
	Snap map[string]*snap.Product
	// Declared N/A paths beyond null leaves.
	Declared []string
}

func ptrNum[T ~int16 | ~int32 | ~int64 | ~float64](v *T) Num {
	if v == nil {
		return nan
	}
	return Num(*v)
}

func decodeInto(raw json.RawMessage, v any, what string) error {
	if len(raw) == 0 {
		return nil
	}
	if err := json.Unmarshal(raw, v); err != nil {
		return fmt.Errorf("%s: %w", what, err)
	}
	return nil
}

func pricingFrom(r *store.PricingRow, plans []store.PlanRow) model.Pricing {
	p := model.Pricing{PricePerGb: nan, PricePerGbAtScale: nan, MinCommit: nan, CostPer1kSuccess: nan, AvgPayloadKb: nan,
		HeadlinePriceRank: nan, ValueRank: nan, Plans: []model.PricingPlan{}}
	if r == nil {
		return p
	}
	p.Model, p.Trial, p.CostBasis = r.Model, r.Trial, r.CostBasis
	p.PricePerGb, p.PricePerGbAtScale, p.MinCommit = ptrNum(r.PricePerGb), ptrNum(r.PricePerGbAtScale), ptrNum(r.MinCommit)
	p.CostPer1kSuccess, p.AvgPayloadKb = ptrNum(r.CostPer1kSuccess), ptrNum(r.AvgPayloadKb)
	p.HeadlinePriceRank, p.ValueRank = ptrNum(r.HeadlinePriceRank), ptrNum(r.ValueRank)
	for _, pl := range plans {
		if pl.ProviderSlug != r.ProviderSlug || pl.Category != r.Category {
			continue
		}
		p.Plans = append(p.Plans, model.PricingPlan{Catalog: pl.Catalog, Name: pl.Name, Price: ptrNum(pl.Price), Gb: ptrNum(pl.Gb), Ips: ptrNum(pl.Ips),
			Note: pl.Note, Recommended: pl.Recommended, DiscountPct: ptrNum(pl.DiscountPct)})
	}
	return p
}

func trustFrom(r *store.TrustRow) (model.TrustSignals, error) {
	t := model.TrustSignals{ResidentialAuthenticity: nan, DatacenterAsnShare: nan, ClaimGap: nan, BlacklistHitRate: nan,
		StickinessHoldRate: nan, LeakChecks: []model.LeakCheck{}}
	if r == nil {
		return t, nil
	}
	t.ResidentialAuthenticity, t.DatacenterAsnShare, t.ClaimGap = ptrNum(r.ResidentialAuthenticity), ptrNum(r.DatacenterAsnShare), ptrNum(r.ClaimGap)
	t.BlacklistHitRate, t.StickinessHoldRate = ptrNum(r.BlacklistHitRate), ptrNum(r.StickinessHoldRate)
	t.StickinessAdvertised, t.KycVerified, t.ConsentDisclosed = r.StickinessAdvertised, r.KycVerified, r.ConsentDisclosed
	if err := decodeInto(r.LeakChecks, &t.LeakChecks, "leak_checks"); err != nil {
		return t, err
	}
	if t.LeakChecks == nil {
		t.LeakChecks = []model.LeakCheck{}
	}
	return t, nil
}

type catalog struct {
	pricing map[[2]string]*store.PricingRow
	trust   map[[2]string]*store.TrustRow
	plans   []store.PlanRow
	prods   map[string][]store.ProductRow
}

func newCatalog(in *store.Inputs) *catalog {
	c := &catalog{pricing: map[[2]string]*store.PricingRow{}, trust: map[[2]string]*store.TrustRow{}, plans: in.Plans,
		prods: map[string][]store.ProductRow{}}
	for i := range in.Pricing {
		r := &in.Pricing[i]
		c.pricing[[2]string{r.ProviderSlug, r.Category}] = r
	}
	for i := range in.Trust {
		r := &in.Trust[i]
		c.trust[[2]string{r.ProviderSlug, r.Category}] = r
	}
	for _, r := range in.Products {
		c.prods[r.ProviderSlug] = append(c.prods[r.ProviderSlug], r)
	}
	return c
}

func (c *catalog) products(slug string) []model.ProviderProduct {
	out := []model.ProviderProduct{}
	for _, r := range c.prods[slug] {
		out = append(out, model.ProviderProduct{Kind: r.Kind, Name: r.Name, Measured: r.Measured, URL: r.URL, PriceLabel: r.PriceLabel, Note: r.Note})
	}
	return out
}

// providerSources indexes the framework identity, the published snapshot and
// the catalog rows by framework provider id.
type providerSources struct {
	catalogBy  map[string]*store.ProviderRow
	unlinked   []*store.ProviderRow
	productsOf map[string][]string
	snapOf     map[string][]*snap.Product
}

func indexProviderSources(in *store.Inputs, s *snap.Snapshot) *providerSources {
	src := &providerSources{catalogBy: map[string]*store.ProviderRow{}, unlinked: []*store.ProviderRow{},
		productsOf: map[string][]string{}, snapOf: map[string][]*snap.Product{}}
	for i := range in.Providers {
		r := &in.Providers[i]
		if r.FrameworkProvider != nil {
			src.catalogBy[*r.FrameworkProvider] = r
		} else {
			src.unlinked = append(src.unlinked, r)
		}
	}
	for _, fp := range in.FrameworkProducts {
		src.productsOf[fp.Provider] = append(src.productsOf[fp.Provider], fp.Name)
	}
	if s != nil {
		for i := range s.Products {
			p := &s.Products[i]
			src.snapOf[p.Provider] = append(src.snapOf[p.Provider], p)
		}
	}
	return src
}

// order lists, sorted, every framework provider with framework products, a
// catalog row or snapshot products.
func (src *providerSources) order(frameworkProviders []string) []string {
	ids := map[string]bool{}
	for _, id := range frameworkProviders {
		if len(src.productsOf[id]) > 0 || src.catalogBy[id] != nil || len(src.snapOf[id]) > 0 {
			ids[id] = true
		}
	}
	for id := range src.snapOf {
		ids[id] = true
	}
	for id := range src.catalogBy {
		ids[id] = true
	}
	order := make([]string, 0, len(ids))
	for id := range ids {
		order = append(order, id)
	}
	sort.Strings(order)
	return order
}

// providerBuild is what every provider's build shares: the snapshot, the
// catalog, the targets and the slugs and names overlap peers are named by.
type providerBuild struct {
	s              *snap.Snapshot
	c              *catalog
	targets        []model.TargetMeta
	slugOfProvider map[string]string
	nameOf         map[string]string
	slugOfProduct  map[int]string
}

// newProviderBuild names every provider first, so overlap peers can be named.
func newProviderBuild(order []string, src *providerSources, s *snap.Snapshot, c *catalog, targets []model.TargetMeta) *providerBuild {
	b := &providerBuild{s: s, c: c, targets: targets, slugOfProvider: map[string]string{}, nameOf: map[string]string{},
		slugOfProduct: map[int]string{}}
	for _, id := range order {
		slug, name := id, ""
		if r := src.catalogBy[id]; r != nil {
			slug, name = r.Slug, r.Name
		}
		if name == "" && len(src.snapOf[id]) > 0 {
			name = src.snapOf[id][0].Name
		}
		if name == "" {
			name = strings.ToUpper(id[:1]) + id[1:]
		}
		b.slugOfProvider[id], b.nameOf[slug] = slug, name
	}
	if s != nil {
		for _, p := range s.Products {
			b.slugOfProduct[int(p.ProductID.F())] = b.slugOfProvider[p.Provider]
		}
	}
	return b
}

// providerIdentity is one provider to build: its framework id ("" for a
// catalog row with no framework link), catalog row, slug, name, snapshot
// products and framework product names.
type providerIdentity struct {
	id        string
	row       *store.ProviderRow
	slug      string
	name      string
	products  []*snap.Product
	framework []string
}

// realProviders builds every framework provider (and every catalog row) from
// the framework identity, the published snapshot and the catalog.
func realProviders(in *store.Inputs, s *snap.Snapshot, c *catalog, targets []model.TargetMeta) ([]*entry, error) {
	src := indexProviderSources(in, s)
	order := src.order(in.FrameworkProviders)
	b := newProviderBuild(order, src, s, c, targets)
	out := []*entry{}
	for _, id := range order {
		row := src.catalogBy[id]
		if row != nil && !row.Listed {
			continue
		}
		slug := b.slugOfProvider[id]
		e, err := b.realProvider(providerIdentity{id: id, row: row, slug: slug, name: b.nameOf[slug],
			products: src.snapOf[id], framework: src.productsOf[id]})
		if err != nil {
			return nil, err
		}
		out = append(out, e)
	}
	for _, row := range src.unlinked {
		if !row.Listed {
			continue
		}
		e, err := b.realProvider(providerIdentity{row: row, slug: row.Slug, name: row.Name})
		if err != nil {
			return nil, err
		}
		out = append(out, e)
	}
	return out, nil
}

func (b *providerBuild) realProvider(pi providerIdentity) (*entry, error) {
	byKind, kinds := snap.ByKind(pi.products)
	primary := primaryKind(pi.row, byKind, kinds, pi.framework)
	ordered := orderedKinds(primary, kinds, byKind)
	p := &model.Provider{Source: model.SourceMeasured, Slug: pi.slug, Name: pi.name, PrimaryKind: primary,
		ProductMeasurements: model.NewMeasurementMap(), Founded: nan, Strengths: []string{}, Weaknesses: []string{}, BestFor: []string{}}
	e := &entry{P: p, Snap: map[string]*snap.Product{}}
	if pi.id != "" {
		p.Framework = &model.FrameworkLink{Provider: pi.id, Products: append([]string{}, pi.framework...)}
	}
	head := b.measureKinds(e, pi, primary, ordered, byKind)
	b.fillHeadline(e, head, pi.slug, primary)
	p.Kinds = append([]string{}, ordered...)
	if len(p.Kinds) == 0 {
		p.Kinds = []string{primary}
	}
	p.Products = measuredProducts(p, ordered, pi.framework, byKind)
	if pi.row != nil {
		b.applyCatalogRow(e, pi.row, head != nil)
	}
	if pi.row == nil || pi.row.BestFor == nil {
		e.Declared = append(e.Declared, "bestFor")
	}
	p.Measured.Normalize()
	return e, nil
}

// primaryKind picks the headline category: the catalog's when it is measured,
// else the snapshot product with the most 30d requests, else the catalog's,
// else the first framework proxy product, else residential.
func primaryKind(row *store.ProviderRow, byKind map[string]*snap.Product, kinds, frameworkProducts []string) string {
	primary := ""
	if row != nil && row.PrimaryKind != nil && byKind[*row.PrimaryKind] != nil {
		primary = *row.PrimaryKind
	}
	if primary == "" {
		best := nan
		for _, k := range kinds {
			n := byKind[k].Window("30d").Metrics.Completed
			if primary == "" || n > best {
				primary, best = k, n
			}
		}
	}
	if primary == "" && row != nil && row.PrimaryKind != nil {
		primary = *row.PrimaryKind
	}
	if primary == "" {
		for _, n := range frameworkProducts {
			if model.IsProxyKind(n) {
				primary = n
				break
			}
		}
	}
	if primary == "" {
		primary = "residential"
	}
	return primary
}

// orderedKinds is the measured categories, headline first.
func orderedKinds(primary string, kinds []string, byKind map[string]*snap.Product) []string {
	ordered := []string{primary}
	for _, k := range kinds {
		if k != primary {
			ordered = append(ordered, k)
		}
	}
	if byKind[primary] == nil {
		ordered = ordered[1:]
	}
	return ordered
}

// measureKinds fills one product measurement per measured category (one
// snapshot product per category) and returns the headline's measured view, nil
// when the headline category is not measured.
func (b *providerBuild) measureKinds(e *entry, pi providerIdentity, primary string, ordered []string, byKind map[string]*snap.Product) *measuredView {
	var head *measuredView
	for _, k := range ordered {
		sp := byKind[k]
		mv := measuredProvider(sp, b.s, b.targets, overlapPeers(b.s, sp, b.slugOfProduct, b.nameOf))
		pm := &model.ProductMeasurement{Kind: k, Product: model.DefaultProduct[k], Measured: mv.Measured}
		if pi.row != nil && k == primary && pi.row.MeasuredProduct != nil && model.ProductCategory[*pi.row.MeasuredProduct] == k {
			pm.Product = *pi.row.MeasuredProduct
		}
		w30 := sp.Window("30d").Metrics
		mergeCatalog(pi.slug, k, &pm.Measured, b.c, &w30)
		e.P.ProductMeasurements.Set(k, pm)
		e.Snap[k] = sp
		if k == primary {
			mvCopy := mv
			head = &mvCopy
			e.Declared = append(e.Declared, mv.Declared...)
		}
	}
	return head
}

// fillHeadline fills the provider's headline measured block, product, score
// and copy from the headline measurement, or catalog-only values without one.
func (b *providerBuild) fillHeadline(e *entry, head *measuredView, slug, primary string) {
	p := e.P
	if head != nil {
		p.Measured = p.ProductMeasurements.Get(primary).Measured
		p.MeasuredProduct = p.ProductMeasurements.Get(primary).Product
		p.Score = head.Score
		p.Tagline, p.Verdict = model.S(head.Tagline), model.S(head.Verdict)
		p.Strengths, p.Weaknesses = head.Strengths, head.Weaknesses
		return
	}
	p.Measured = model.EmptyMeasured()
	mergeCatalog(slug, primary, &p.Measured, b.c, nil)
	p.MeasuredProduct = model.DefaultProduct[primary]
	p.Score = model.ScoreView{Composite: nan, Rank: nan, PreviousRank: nan, Breakdown: model.NewBreakdown()}
	e.Declared = append(e.Declared, "productMeasurements", "strengths", "weaknesses")
}

// measuredProducts lists the measured products, headline first, then the
// framework's unmeasured proxy products.
func measuredProducts(p *model.Provider, ordered, frameworkProducts []string, byKind map[string]*snap.Product) []model.ProviderProduct {
	out := []model.ProviderProduct{}
	for _, k := range ordered {
		pp := model.ProviderProduct{Kind: p.ProductMeasurements.Get(k).Product, Name: model.S(model.KindLabels[k]), Measured: true}
		// The tested pool's list rates, lowest first (format.ts priceRange).
		if pr := p.ProductMeasurements.Get(k).Pricing; pr.PricePerGb.OK() || pr.PricePerGbAtScale.OK() {
			pp.PriceLabel = model.S(jsnum.PriceRange(pr.PricePerGbAtScale.F(), pr.PricePerGb.F()))
		}
		out = append(out, pp)
	}
	for _, n := range frameworkProducts {
		if model.IsProxyKind(n) && byKind[n] == nil {
			out = append(out, model.ProviderProduct{Kind: model.DefaultProduct[n], Name: model.S(model.KindLabels[n]), Measured: false})
		}
	}
	return out
}

// applyCatalogRow lets the catalog row's identity, copy and products win over
// the measured ones; measured says whether a headline measurement exists.
func (b *providerBuild) applyCatalogRow(e *entry, row *store.ProviderRow, measured bool) {
	p := e.P
	if row.Tagline != nil {
		p.Tagline = row.Tagline
	}
	if len(row.Kinds) > 0 {
		p.Kinds = row.Kinds
	}
	if row.MeasuredProduct != nil && !measured {
		p.MeasuredProduct = *row.MeasuredProduct
	}
	p.HQ, p.Founded, p.Claim, p.Website = row.HQ, ptrNum(row.Founded), row.Claim, row.Website
	if row.Verdict != nil {
		p.Verdict = row.Verdict
	}
	if row.Strengths != nil {
		p.Strengths = row.Strengths
		e.Declared = remove(e.Declared, "strengths")
	}
	if row.Weaknesses != nil {
		p.Weaknesses = row.Weaknesses
		e.Declared = remove(e.Declared, "weaknesses")
	}
	if row.BestFor != nil {
		p.BestFor = row.BestFor
	}
	if prods := b.c.labelledProducts(row.Slug, p.Products); len(prods) > 0 {
		p.Products = prods
	}
}

// labelledProducts is the catalog's products for slug. A catalog link must not
// discard a measured product's current list-rate label: a catalog product
// without a label takes the first measured product of its kind's.
func (c *catalog) labelledProducts(slug string, measured []model.ProviderProduct) []model.ProviderProduct {
	prods := c.products(slug)
	for i := range prods {
		if prods[i].PriceLabel != nil {
			continue
		}
		for _, m := range measured {
			if m.Kind == prods[i].Kind {
				prods[i].PriceLabel = m.PriceLabel
				break
			}
		}
	}
	return prods
}

func remove(list []string, v string) []string {
	out := list[:0]
	for _, x := range list {
		if x != v {
			out = append(out, x)
		}
	}
	return out
}

// mergeCatalog fills catalog pricing and trust into a measured block. Measured
// values win (the snapshot's list prices, wire-byte cost and authenticity);
// catalog values fill what the snapshot does not measure. w30 is the 30d
// snapshot metrics the cost per 1,000 successes is derived from (nil without
// a snapshot product).
func mergeCatalog(slug, kind string, m *model.Measured, c *catalog, w30 *snap.Metrics) {
	pr := pricingFrom(c.pricing[[2]string{slug, kind}], c.plans)
	measured := m.Pricing
	if measured.PricePerGb.OK() || measured.PricePerGbAtScale.OK() {
		pr.PricePerGb, pr.PricePerGbAtScale = measured.PricePerGb, measured.PricePerGbAtScale
		if pr.Model == nil {
			pr.Model = measured.Model
		}
	}
	if !pr.CostPer1kSuccess.OK() && w30 != nil {
		if cost := wireCostPer1k(listRate(pr), *w30); cost.OK() {
			pr.CostPer1kSuccess = cost
			if pr.CostBasis == nil {
				pr.CostBasis = model.S(costBasisText(listRate(pr), *w30))
			}
		}
	}
	if !pr.CostPer1kSuccess.OK() {
		if cost, basis := costPer1k(pr.PricePerGb, pr.AvgPayloadKb, m.Performance.SuccessRate); cost.OK() {
			pr.CostPer1kSuccess = cost
			if pr.CostBasis == nil {
				pr.CostBasis = model.S(basis)
			}
		}
	}
	m.Pricing = pr
	if r := c.trust[[2]string{slug, kind}]; r != nil && r.ClaimedPoolSize != nil {
		m.Pool.ClaimedPoolSize = Num(*r.ClaimedPoolSize)
	}
	if t, err := trustFrom(c.trust[[2]string{slug, kind}]); err == nil {
		if m.Trust.DatacenterAsnShare.OK() {
			t.DatacenterAsnShare = m.Trust.DatacenterAsnShare
		}
		if m.Trust.ClaimGap.OK() {
			t.ClaimGap = m.Trust.ClaimGap
		}
		if m.Trust.ResidentialAuthenticity.OK() {
			t.ResidentialAuthenticity = m.Trust.ResidentialAuthenticity
		}
		m.Trust = t
	}
}
