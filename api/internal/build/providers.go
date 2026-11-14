package build

import (
	"sort"
	"strings"

	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/snap"
	"github.com/Zeus-Development/atlas/api/internal/store"
)

// entry is a built provider plus what derived views need.
type entry struct {
	P *model.Provider
	// Snapshot products per category (measured providers), headline first.
	Snap map[string]*snap.Product
	// Declared N/A paths beyond null leaves.
	Declared []string
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
		ProductMeasurements: model.NewMeasurementMap(), Founded: nan, UseCases: []string{}}
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
	if pi.row == nil || pi.row.UseCases == nil {
		e.Declared = append(e.Declared, "useCases")
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
		fillPricingAllTargets(&pm.Measured.Pricing, sp, cardTargets(b.targets))
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
		return
	}
	p.Measured = model.EmptyMeasured()
	mergeCatalog(slug, primary, &p.Measured, b.c, nil)
	p.MeasuredProduct = model.DefaultProduct[primary]
	p.Score = model.ScoreView{Composite: nan, Rank: nan, PreviousRank: nan, Breakdown: model.NewBreakdown()}
	e.Declared = append(e.Declared, "productMeasurements")
}

// measuredProducts lists the measured products, headline first, then the
// framework's unmeasured proxy products.
func measuredProducts(p *model.Provider, ordered, frameworkProducts []string, byKind map[string]*snap.Product) []model.ProviderProduct {
	out := []model.ProviderProduct{}
	for _, k := range ordered {
		out = append(out, model.ProviderProduct{Kind: p.ProductMeasurements.Get(k).Product, Measured: true})
	}
	for _, n := range frameworkProducts {
		if model.IsProxyKind(n) && byKind[n] == nil {
			out = append(out, model.ProviderProduct{Kind: model.DefaultProduct[n], Measured: false})
		}
	}
	return out
}

// applyCatalogRow lets the catalog row's identity and products win over the
// measured ones; measured says whether a headline measurement exists. Its
// English copy (tagline, claim, verdict, strengths, weaknesses) is not served
// (API-PROSE-REMOVAL-1, #962).
func (b *providerBuild) applyCatalogRow(e *entry, row *store.ProviderRow, measured bool) {
	p := e.P
	if len(row.Kinds) > 0 {
		p.Kinds = row.Kinds
	}
	if row.MeasuredProduct != nil && !measured {
		p.MeasuredProduct = *row.MeasuredProduct
	}
	p.Founded, p.Website = ptrNum(row.Founded), row.Website
	p.HQCountry, p.HQCity = row.HQCountry, row.HQCity
	if row.UseCases != nil {
		p.UseCases = row.UseCases
	}
	if prods := b.c.products(row.Slug); len(prods) > 0 {
		p.Products = prods
	}
}
