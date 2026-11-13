package build

import (
	"encoding/json"
	"slices"
	"strings"
	"testing"

	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/testscale"
)

// searchIndex is an index of six residential networks (the second's slug
// holds the first's name), a card target, an AI source target and a
// catalog-only target.
func searchIndex() *SearchIndex {
	str := func(s string) model.Str { return &s }
	rows := []RankingRow{{Slug: "alpha-net", Name: "Alpha Net"}, {Slug: "alpha-bravo", Name: "Bravo"}}
	for _, slug := range []string{"c", "d", "e", "f"} {
		rows = append(rows, RankingRow{Slug: slug, Name: strings.ToUpper(slug)})
	}
	targets := []model.TargetMeta{
		{ID: "amazon_pdp", Label: str("Amazon product page"), Category: str("E-commerce"), Source: model.SourceMeasured},
		{ID: "ai_overview", Label: str("AI Overview"), Group: str("ai_answers"), Source: model.SourceMeasured},
		{ID: "catalog_only", Label: str("Catalog only"), Source: "catalog"},
	}
	return buildSearchIndex(rows, targets)
}

func ids(items []SearchItem) string {
	out := []string{}
	for _, it := range items {
		out = append(out, it.Type+":"+it.ID)
	}
	return strings.Join(out, " ")
}

// An answer is the query, the total and typed items: a network's id is its
// slug and its name its name; a target's name is null. No palette field is
// left.
func TestASearchAnswersTypedItems(t *testing.T) {
	t.Parallel()
	body, err := json.Marshal(searchIndex().Search("amazon"))
	if err != nil {
		t.Fatal(err)
	}
	if want := `{"query":"amazon","total":1,"items":[{"type":"target","id":"amazon_pdp","name":null}]}`; string(body) != want {
		t.Errorf("amazon: %s, want %s", body, want)
	}
	body, _ = json.Marshal(searchIndex().Search("bravo"))
	if want := `{"query":"bravo","total":1,"items":[{"type":"network","id":"alpha-bravo","name":"Bravo"}]}`; string(body) != want {
		t.Errorf("bravo: %s, want %s", body, want)
	}
}

// A name match ranks over a slug or id match; only card targets are
// searched, by their id's words, never by their English label, category or
// pass condition.
func TestASearchRanksANameOverAnID(t *testing.T) {
	t.Parallel()
	idx := searchIndex()
	if got := ids(idx.Search("alpha").Items); got != "network:alpha-net network:alpha-bravo" {
		t.Errorf("alpha: %s", got)
	}
	if got := ids(idx.Search("pdp").Items); got != "target:amazon_pdp" {
		t.Errorf("pdp: %s", got)
	}
	for _, q := range []string{"product", "commerce", "overview", "catalog", "networks", "dataset", "7d"} {
		if got := idx.Search(q); got.Total != 0 || len(got.Items) != 0 {
			t.Errorf("search %q finds %s", q, ids(got.Items))
		}
	}
}

// An empty query answers the ranking's first five networks.
func TestAnEmptySearchAnswersTheTopNetworks(t *testing.T) {
	t.Parallel()
	got := searchIndex().Search("  ")
	if ids(got.Items) != "network:alpha-net network:alpha-bravo network:c network:d network:e" || got.Total != 5 {
		t.Errorf("empty search: %+v", got)
	}
	empty := buildSearchIndex(nil, nil).Search("")
	if body, _ := json.Marshal(empty); string(body) != `{"query":"","total":0,"items":[]}` {
		t.Errorf("no networks: %s", body)
	}
}

// The palette's weights: the name at its start 120 or further along 90 less
// its position, a word start of the slug, name or id 55, then a
// dropped-letter name match (18) whose letters span less than twice the
// token; plus 6 for a network and 2 for a target.
func TestSearchWeighsLikeThePalette(t *testing.T) {
	t.Parallel()
	net := func(slug, name string) SearchItem {
		return buildSearchIndex([]RankingRow{{Slug: slug, Name: name}}, nil).items[0]
	}
	for _, c := range []struct {
		it    SearchItem
		token string
		want  int
	}{
		{net("x", "Speedy"), "speedy", 126}, {net("x", "So speedy"), "speedy", 6 + 90 - 3},
		{net("speedy-x", "Fast"), "speedy", 61}, {net("x", "a.b.c.d"), "abcd", 24}, {net("x", "a..b..c..d"), "abcd", 0},
		{SearchItem{Type: SearchTarget, words: terms("speedy_x")}, "x", 57},
	} {
		if v := c.it.score([]string{c.token}); v != c.want {
			t.Errorf("%+v %q: %d, want %d", c.it, c.token, v, c.want)
		}
	}
}

// A build's index lists the 30-day residential ranking's networks in its
// order, then the card targets.
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
	want := []string{}
	for _, r := range res.Pages.RankingsByKind[searchRanking].Networks {
		want = append(want, r.Slug)
	}
	nets := []string{}
	for _, it := range res.Search.items {
		if it.Type == SearchNetwork {
			nets = append(nets, it.ID)
		}
	}
	if len(want) == 0 || !slices.Equal(nets, want) {
		t.Errorf("networks %v, want %v", nets, want)
	}
	if found := res.Search.Search(want[0]); len(found.Items) == 0 || found.Items[0].ID != want[0] {
		t.Fatalf("search %s: %+v", want[0], found)
	}
}
