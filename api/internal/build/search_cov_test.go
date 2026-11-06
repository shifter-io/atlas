package build

import (
	"fmt"
	"strings"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/testscale"
)

// An empty query answers the palette's default sections: the starting
// points, then the top networks.
func TestAnEmptySearchAnswersTheDefaultSections(t *testing.T) {
	in, err := testscale.Inputs(testscale.Shape{Networks: 2, Countries: 1, Exits: 3, Tail: 30})
	if err != nil {
		t.Fatal(err)
	}
	res, err := Build(in)
	if err != nil {
		t.Fatal(err)
	}
	got := res.Search.Search("  ")
	if len(got.Sections) != 2 || got.Sections[0].Group != "Jump to" || got.Sections[1].Group != "Top networks" ||
		len(got.Sections[0].Items) != len(startingPointIDs) || len(got.Sections[1].Items) != 2 ||
		got.Total != len(startingPointIDs)+2 || len(got.Tokens) != 0 {
		t.Fatalf("empty search: %+v", got)
	}
	if got.Sections[1].Items[0].ID != res.Search.TopNetworkIDs[0] {
		t.Errorf("top network %s, want %s", got.Sections[1].Items[0].ID, res.Search.TopNetworkIDs[0])
	}
}

// Ids not in the index are skipped, and a section with no items is left out.
func TestDefaultSectionsSkipUnknownIDs(t *testing.T) {
	idx := searchIndexOf(nil)
	idx.StartingPointIDs, idx.TopNetworkIDs = []string{"page:/gone"}, []string{"network:gone"}
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

// A query matching more than MaxResults items takes at most PerGroup of each
// group, best first, groups in the order they first appear.
func TestSearchCapsTheResultsAndEachGroup(t *testing.T) {
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
	if got.Total != MaxResults || strings.Join(got.Tokens, " ") != "alpha" {
		t.Fatalf("search: total %d, tokens %v", got.Total, got.Tokens)
	}
	sections := []string{}
	for _, sec := range got.Sections {
		sections = append(sections, fmt.Sprintf("%s=%d", sec.Group, len(sec.Items)))
	}
	if strings.Join(sections, " ") != fmt.Sprintf("A=%d B=%d C=%d D=%d", PerGroup, PerGroup, PerGroup, MaxResults-3*PerGroup) {
		t.Fatalf("sections %v", sections)
	}
	if a := got.Sections[0].Items; a[0].ID != "A:0" || a[PerGroup-1].ID != fmt.Sprintf("A:%d", PerGroup-1) {
		t.Errorf("group A keeps index order: %+v", a)
	}
}
