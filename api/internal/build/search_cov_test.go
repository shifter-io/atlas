package build

import (
	"fmt"
	"strings"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/testscale"
)

// An empty query answers the palette's "Top networks"; its "Recent" and
// "Jump to" are website-local.
func TestAnEmptySearchAnswersTheTopNetworks(t *testing.T) {
	in, err := testscale.Inputs(testscale.Shape{Networks: 2, Countries: 1, Exits: 3, Tail: 30})
	if err != nil {
		t.Fatal(err)
	}
	res, err := Build(in)
	if err != nil {
		t.Fatal(err)
	}
	got := res.Search.Search("  ")
	if len(got.Sections) != 1 || got.Sections[0].Group != "Top networks" || len(got.Sections[0].Items) != 2 ||
		got.Total != 2 || len(got.Tokens) != 0 {
		t.Fatalf("empty search: %+v", got)
	}
	if got.Sections[0].Items[0].ID != res.Search.TopNetworkIDs[0] {
		t.Errorf("top network %s, want %s", got.Sections[0].Items[0].ID, res.Search.TopNetworkIDs[0])
	}
}

// Ids not in the index are skipped, and a section with no items is left out.
func TestDefaultSectionsSkipUnknownIDs(t *testing.T) {
	idx := searchIndexOf(nil)
	idx.TopNetworkIDs = []string{"net:gone"}
	if got := idx.Search(""); len(got.Sections) != 0 || got.Total != 0 {
		t.Fatalf("search: %+v", got)
	}
}

// searchIndexOf is an index of items as buildSearchIndex keeps them.
func searchIndexOf(items []SearchItem) *SearchIndex {
	idx := &SearchIndex{Items: items, GroupBonus: GroupBonus, byID: map[string]*SearchItem{}}
	for i := range idx.Items {
		it := &idx.Items[i]
		it.lowTitle, it.lowSubtitle, it.lowKeywords = strings.ToLower(it.Title), strings.ToLower(it.Subtitle), strings.ToLower(it.Keywords)
		idx.byID[it.ID] = it
	}
	return idx
}

// A query takes at most PerGroup of each group, best first, groups in the
// order they first appear; like the palette, there is no cap on the total.
func TestSearchCapsEachGroup(t *testing.T) {
	items := []SearchItem{}
	// Group A's titles start with the token (120); B, C and D find it further
	// along (90 less its position: 88, 87, 86). None of them has a bonus.
	for _, g := range []struct {
		group, prefix string
		n             int
	}{{"A", "", 8}, {"B", "b ", 6}, {"C", "cc ", 6}, {"D", "ddd ", 6}} {
		for i := range g.n {
			items = append(items, SearchItem{ID: fmt.Sprintf("%s:%d", g.group, i), Group: g.group, Title: fmt.Sprintf("%salpha %d", g.prefix, i)})
		}
	}
	got := searchIndexOf(items).Search("Alpha")
	if got.Total != 4*PerGroup || strings.Join(got.Tokens, " ") != "alpha" {
		t.Fatalf("search: total %d, tokens %v", got.Total, got.Tokens)
	}
	sections := []string{}
	for _, sec := range got.Sections {
		sections = append(sections, fmt.Sprintf("%s=%d", sec.Group, len(sec.Items)))
	}
	if strings.Join(sections, " ") != fmt.Sprintf("A=%d B=%d C=%d D=%d", PerGroup, PerGroup, PerGroup, PerGroup) {
		t.Fatalf("sections %v", sections)
	}
	if a := got.Sections[0].Items; a[0].ID != "A:0" || a[PerGroup-1].ID != fmt.Sprintf("A:%d", PerGroup-1) {
		t.Errorf("group A keeps index order: %+v", a)
	}
}

// The palette's weights: a keyword at a word start (55) before the subtitle
// (45), then a dropped-letter title match (18) whose letters span less than
// twice the token.
func TestSearchWeighsLikeThePalette(t *testing.T) {
	idx := searchIndexOf([]SearchItem{
		{ID: "sub", Group: "X", Title: "one", Subtitle: "a speedy pool"},
		{ID: "key", Group: "X", Title: "two", Keywords: "fast speedy"},
		{ID: "near", Group: "X", Title: "a.b.c.d"},
		{ID: "far", Group: "X", Title: "a..b..c..d"},
	})
	got := idx.Search("speedy")
	if len(got.Sections) != 1 || len(got.Sections[0].Items) != 2 || got.Sections[0].Items[0].ID != "key" {
		t.Fatalf("speedy: %+v", got.Sections)
	}
	if v := idx.byID["key"].score([]string{"speedy"}); v != 55 {
		t.Errorf("keyword score %d", v)
	}
	if v := idx.byID["sub"].score([]string{"speedy"}); v != 45 {
		t.Errorf("subtitle score %d", v)
	}
	if v := idx.byID["near"].score([]string{"abcd"}); v != 18 {
		t.Errorf("loose match spanning 7 letters: %d", v)
	}
	if v := idx.byID["far"].score([]string{"abcd"}); v != 0 {
		t.Errorf("loose match spanning 10 letters: %d", v)
	}
}
