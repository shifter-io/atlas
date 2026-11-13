package build

import (
	"maps"
	"math"
	"slices"
	"strings"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/Zeus-Development/proxy-index/api/internal/testscale"
)

// paletteIndex is an index of six residential networks (the first scored and
// ranked, the second neither), a card target, an AI source target and a
// catalog-only target.
func paletteIndex() *SearchIndex {
	str := func(s string) model.Str { return &s }
	rows := []RankingRow{
		{Slug: "alpha-net", Name: "Alpha Net", Score: 81.24, Standing: Standing{Rank: 1}},
		{Slug: "beta", Name: "Beta", Score: Num(math.NaN()), Standing: Standing{Rank: Num(math.NaN())}},
	}
	for _, slug := range []string{"c", "d", "e", "f"} {
		rows = append(rows, RankingRow{Slug: slug, Name: strings.ToUpper(slug), Score: 50, Standing: Standing{Rank: 3}})
	}
	targets := []model.TargetMeta{
		{ID: "generic_https", Label: str("Generic HTTPS"), Category: str("Baseline"), PassCondition: str("Answers 200 over TLS"), Source: model.SourceMeasured},
		{ID: "ai_overview", Label: str("AI Overview"), Group: str("ai_answers"), Source: model.SourceMeasured},
		{ID: "catalog_only", Label: str("Catalog only"), Source: "catalog"},
	}
	return buildSearchIndex(rows, targets, "2026-11-09T00:00:00Z")
}

// Every item is the website palette's: its ids, its /proxy-index links, a
// network's "score · #rank" without a subtitle (no tagline, #962), the card targets only, and no old
// site page, country or action.
func TestTheSearchIndexIsTheWebsitePalettes(t *testing.T) {
	t.Parallel()
	idx := paletteIndex()
	want := map[string]string{
		"net:alpha-net": "/proxy-index/networks/alpha-net", "net:beta": "/proxy-index/networks/beta",
		"net:c": "/proxy-index/networks/c", "net:d": "/proxy-index/networks/d", "net:e": "/proxy-index/networks/e", "net:f": "/proxy-index/networks/f",
		"page:/proxy-index": "/proxy-index", "page:/proxy-index/networks": "/proxy-index/networks",
		"page:/proxy-index/compare": "/proxy-index/compare", "page:/proxy-index/overlap": "/proxy-index/overlap",
		"page:/proxy-index/true-cost": "/proxy-index/true-cost", "page:/proxy-index/live-tape": "/proxy-index/live-tape",
		"page:/proxy-index/methodology": "/proxy-index/methodology", "page:/proxy-index/targets": "/proxy-index/targets",
		"target:generic_https": "/proxy-index/targets#generic_https",
		"act:dataset":          "/proxy-index/dataset.json",
		"act:range:24h":        "/proxy-index", "act:range:7d": "/proxy-index", "act:range:30d": "/proxy-index",
	}
	got := map[string]string{}
	groups := []string{}
	for _, it := range idx.Items {
		got[it.ID] = it.Href
		if !slices.Contains(groups, it.Group) {
			groups = append(groups, it.Group)
		}
		if strings.Contains(it.Subtitle+it.Title, "MILF") {
			t.Errorf("%s still has the old blurb: %+v", it.ID, it)
		}
	}
	if len(got) != len(want) {
		t.Errorf("%d items, want %d: %v", len(got), len(want), got)
	}
	for id, href := range want {
		if got[id] != href {
			t.Errorf("%s links %q, want %q", id, got[id], href)
		}
	}
	if strings.Join(groups, " ") != "Networks Pages Targets Actions" {
		t.Errorf("groups %v", groups)
	}
	alpha, beta := idx.byID["net:alpha-net"], idx.byID["net:beta"]
	if alpha.Subtitle != "" || *alpha.Meta != "81.2 · #1" || alpha.Title != "Alpha Net" || alpha.Keywords != "alpha net alpha net" {
		t.Errorf("alpha-net: %+v (meta %s)", alpha, *alpha.Meta)
	}
	if beta.Subtitle != "" || *beta.Meta != "N/A · Unranked" {
		t.Errorf("beta: %+v (meta %s)", beta, *beta.Meta)
	}
	target := idx.byID["target:generic_https"]
	if target.Title != "Generic HTTPS" || target.Subtitle != "Answers 200 over TLS" || target.Meta != nil ||
		target.Keywords != "generic https baseline answers 200 over tls generic https target website site" {
		t.Errorf("target: %+v", target)
	}
	if overview := idx.byID["page:/proxy-index"]; overview.Title != "Overview" || overview.Subtitle != "Rankings, market figures and the live dashboard" {
		t.Errorf("overview: %+v", overview)
	}
	if ds := idx.byID["act:dataset"]; !ds.Download || ds.Command != nil {
		t.Errorf("dataset action: %+v", ds)
	}
	if r := idx.byID["act:range:7d"]; r.Title != "Show the last 7 days" || r.Command == nil || *r.Command != "range:7d" || r.Download {
		t.Errorf("range action: %+v", r)
	}
	if strings.Join(idx.TopNetworkIDs, " ") != "net:alpha-net net:beta net:c net:d net:e" {
		t.Errorf("top networks %v", idx.TopNetworkIDs)
	}
}

// The palette's constants: its group bonuses (Sections and Guides included,
// for the items the website adds) and five per group.
func TestTheSearchIndexHasThePalettesConstants(t *testing.T) {
	t.Parallel()
	idx := paletteIndex()
	want := map[string]int{"Networks": 6, "Pages": 5, "Sections": 3, "Guides": 2, "Targets": 2, "Actions": 1}
	if !maps.Equal(idx.GroupBonus, want) || idx.PerGroup != 5 {
		t.Errorf("groupBonus %v, perGroup %d", idx.GroupBonus, idx.PerGroup)
	}
}

// A query per group finds that group's item first.
func TestASearchFindsEachGroup(t *testing.T) {
	t.Parallel()
	idx := paletteIndex()
	for q, want := range map[string][2]string{
		"alpha":     {"Networks", "net:alpha-net"},
		"resellers": {"Pages", "page:/proxy-index/overlap"},
		"true cost": {"Pages", "page:/proxy-index/true-cost"},
		"https":     {"Targets", "target:generic_https"},
		"tls":       {"Targets", "target:generic_https"},
		"dataset":   {"Actions", "act:dataset"},
		"7d":        {"Actions", "act:range:7d"},
	} {
		got := idx.Search(q)
		if len(got.Sections) == 0 || got.Sections[0].Group != want[0] || got.Sections[0].Items[0].ID != want[1] {
			t.Errorf("search %q: %+v", q, got.Sections)
		}
	}
	for _, q := range []string{"apply", "theme", "germany", "milf"} {
		if got := idx.Search(q); got.Total != 0 {
			t.Errorf("search %q finds %+v", q, got.Sections)
		}
	}
}

// A build's index lists the 30-day residential ranking's networks in its
// order, and links every item into the Proxy Index.
func TestABuildsSearchIndexIsTheResidentialRanking(t *testing.T) {
	t.Parallel()
	in, err := testscale.Inputs(testscale.Shape{Networks: 2, Countries: 2, Exits: 3, Tail: 60})
	if err != nil {
		t.Fatal(err)
	}
	res, err := Build(in)
	if err != nil {
		t.Fatal(err)
	}
	rows := res.Pages.RankingsByKind[searchRanking].Networks
	want := []string{}
	for _, r := range rows {
		want = append(want, "net:"+r.Slug)
	}
	nets := []string{}
	for _, it := range res.Search.Items {
		if it.Group == "Networks" {
			nets = append(nets, it.ID)
		}
		if !strings.HasPrefix(it.Href, "/proxy-index") {
			t.Errorf("%s links %s", it.ID, it.Href)
		}
	}
	if len(want) == 0 || !slices.Equal(nets, want) {
		t.Errorf("networks %v, want %v", nets, want)
	}
	found := res.Search.Search(rows[0].Slug)
	if len(found.Sections) == 0 || found.Sections[0].Items[0].ID != want[0] {
		t.Fatalf("search %s: %+v", rows[0].Slug, found)
	}
}
