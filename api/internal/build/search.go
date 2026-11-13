package build

import (
	"slices"
	"sort"
	"strings"
	"unicode"
	"unicode/utf8"

	"github.com/Zeus-Development/proxy-index/api/internal/model"
)

// /v1/search (SEARCH-NEUTRAL-1, #981): the networks and card targets a query
// matches, as types, ids and names; the website's palette writes its own
// words. Its ranking is the palette's (pi-palette.tsx) on a network's name
// and slug and a target id's words.

// The types of a search item (the code set search_item_type).
const (
	SearchNetwork = "network"
	SearchTarget  = "target"
)

// SearchItem is one /v1/search item: a network (id its slug, name its name)
// or a card target (id the target id, name null: its words are the site's).
type SearchItem struct {
	Type string  `json:"type"`
	ID   string  `json:"id"`
	Name *string `json:"name"`

	lowName, words string
}

// SearchIndex is what /v1/search ranks: the networks of the 30-day
// residential ranking in its order, then the card targets.
type SearchIndex struct {
	items    []SearchItem
	networks int
}

const (
	// topNetworks is what an empty query answers: the ranking's first five.
	topNetworks = 5
	// searchRanking is the ranking the networks come from: the residential
	// networks (pi-data.ts MEASURED) of the 30-day leaderboard.
	searchRanking = "30d/residential"
)

// typeBonus is the palette's group bonus of its networks and targets.
var typeBonus = map[string]int{SearchNetwork: 6, SearchTarget: 2}

func sp(s string) *string { return &s }

// terms is pi-palette.tsx terms(): the parts lowercased, every run of
// characters that are not letters or digits one space.
func terms(parts ...string) string {
	f := func(r rune) bool { return !unicode.IsLetter(r) && !unicode.IsNumber(r) }
	return strings.Join(strings.FieldsFunc(strings.ToLower(strings.Join(parts, " ")), f), " ")
}

// buildSearchIndex is the residential ranking's networks and the card targets.
func buildSearchIndex(networks []RankingRow, targets []model.TargetMeta) *SearchIndex {
	idx := &SearchIndex{items: []SearchItem{}, networks: len(networks)}
	for _, row := range networks {
		idx.items = append(idx.items, SearchItem{Type: SearchNetwork, ID: row.Slug, Name: sp(row.Name),
			lowName: strings.ToLower(row.Name), words: terms(row.Slug, row.Name)})
	}
	for _, t := range targets {
		if isCard(t) {
			idx.items = append(idx.items, SearchItem{Type: SearchTarget, ID: t.ID, words: terms(t.ID)})
		}
	}
	return idx
}

// looseMatch is the palette's dropped-letter match, on runes.
func looseMatch(needle, haystack []rune) bool {
	if len(needle) < 4 {
		return false
	}
	i, start := 0, -1
	for at := 0; at < len(haystack); at++ {
		if haystack[at] != needle[i] {
			continue
		}
		if i == 0 {
			start = at
		}
		i++
		if i == len(needle) {
			return at-start < len(needle)*2
		}
	}
	return false
}

func startsWord(haystack, token string) bool {
	return strings.HasPrefix(haystack, token) || strings.Contains(haystack, " "+token)
}

// runeIndex is indexOf in characters.
func runeIndex(s, sub string) int {
	i := strings.Index(s, sub)
	if i < 0 {
		return -1
	}
	return utf8.RuneCountInString(s[:i])
}

// score is the palette's: every token must match; the name at its start 120,
// further along 90 less its position, a word start of the slug, name or
// target id 55, a dropped-letter name match 18; plus the type's bonus.
func (it *SearchItem) score(tokens []string) int {
	total := 0
	for _, t := range tokens {
		at := runeIndex(it.lowName, t)
		switch {
		case at == 0:
			total += 120
		case at > 0:
			total += 90 - min(at, 40)
		case startsWord(it.words, t):
			total += 55
		case looseMatch([]rune(t), []rune(it.lowName)):
			total += 18
		default:
			return 0
		}
	}
	return total + typeBonus[it.Type]
}

// SearchResult is /v1/search.
type SearchResult struct {
	Query string       `json:"query"`
	Total int          `json:"total"`
	Items []SearchItem `json:"items"`
}

// Tokens normalises a query the way the palette does.
func Tokens(q string) []string {
	return strings.Fields(strings.ToLower(q))
}

// Search is every item matching each word of q, best first (ties keep the
// index's order); an empty q answers the ranking's first five networks.
func (idx *SearchIndex) Search(q string) SearchResult {
	tokens := Tokens(q)
	res := SearchResult{Query: q}
	if len(tokens) == 0 {
		res.Items = slices.Clone(idx.items[:min(idx.networks, topNetworks)])
	} else {
		res.Items = idx.ranked(tokens)
	}
	res.Total = len(res.Items)
	return res
}

// ranked are the items matching every token, best first.
func (idx *SearchIndex) ranked(tokens []string) []SearchItem {
	type hit struct {
		it    SearchItem
		value int
	}
	hits := []hit{}
	for i := range idx.items {
		if v := idx.items[i].score(tokens); v > 0 {
			hits = append(hits, hit{idx.items[i], v})
		}
	}
	sort.SliceStable(hits, func(i, j int) bool { return hits[i].value > hits[j].value })
	items := make([]SearchItem, len(hits))
	for i, h := range hits {
		items[i] = h.it
	}
	return items
}
