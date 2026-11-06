package build

import (
	"strings"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/testscale"
)

// The search index covers networks, countries, pages, targets and actions
// only: no articles (guides or network reviews), no Guides group and no link
// to a page only the old website had.
func TestTheSearchIndexHasNoArticles(t *testing.T) {
	t.Parallel()
	in, err := testscale.Inputs(testscale.Shape{Networks: 2, Countries: 2, Exits: 3, Tail: 60})
	if err != nil {
		t.Fatal(err)
	}
	res, err := Build(in)
	if err != nil {
		t.Fatal(err)
	}
	idx := res.Search
	if _, ok := idx.GroupBonus["Guides"]; ok {
		t.Errorf("groupBonus still has a Guides group: %v", idx.GroupBonus)
	}
	groups := map[string]int{}
	pages := []string{}
	for _, it := range idx.Items {
		groups[it.Group]++
		if it.Group == "Guides" || strings.HasPrefix(it.ID, "guide:") || strings.HasPrefix(it.ID, "review:") {
			t.Errorf("article in the search index: %+v", it)
		}
		if it.Download || strings.HasPrefix(it.Href, "/data/") || it.ID == "action:dataset" {
			t.Errorf("link to the old website's dataset file: %+v", it)
		}
		if it.Group == "Pages" {
			pages = append(pages, it.Href)
		}
		if _, ok := idx.GroupBonus[it.Group]; !ok {
			t.Errorf("%s is in group %q, which has no bonus", it.ID, it.Group)
		}
	}
	for _, g := range []string{"Networks", "Countries", "Pages", "Actions"} {
		if groups[g] == 0 {
			t.Errorf("no %s in the search index (groups %v)", g, groups)
		}
	}
	if got := strings.Join(pages, " "); got != "/ /networks /compare /overlap /pricing /targets /methodology /changelog" {
		t.Errorf("page links %s", got)
	}
	for _, id := range idx.StartingPointIDs {
		if _, ok := idx.byID[id]; !ok {
			t.Errorf("starting point %s is not in the index", id)
		}
	}
	if got := strings.Join(idx.StartingPointIDs, " "); got != "page:/networks page:/compare page:/overlap page:/pricing" {
		t.Errorf("starting points %s", got)
	}
	found := idx.Search("net00")
	if len(found.Sections) == 0 || found.Sections[0].Group != "Networks" || found.Sections[0].Items[0].ID != "network:net00" {
		t.Fatalf("search net00: %+v", found)
	}
}
