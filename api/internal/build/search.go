package build

import (
	"sort"
	"strings"
	"unicode"
	"unicode/utf8"

	"github.com/Zeus-Development/proxy-index/api/internal/jsnum"
	"github.com/Zeus-Development/proxy-index/api/internal/model"
)

// SearchItem is one palette item, with pi-palette.tsx's ids (net:, page:,
// target:, act:); every field is always written. Command is what the website
// runs instead of following Href, and Download marks a file to download.
type SearchItem struct {
	ID       string  `json:"id"`
	Group    string  `json:"group"`
	Title    string  `json:"title"`
	Subtitle string  `json:"subtitle"`
	Href     string  `json:"href"`
	Icon     string  `json:"icon"`
	Meta     *string `json:"meta"`
	Keywords string  `json:"keywords"`
	Command  *string `json:"command"`
	Download bool    `json:"download"`

	lowTitle, lowSubtitle, lowKeywords string
}

// SearchIndex is /v1/search-index: the server half of the website's
// palette (pi-palette.tsx). Sections and guides stay website-local; the
// website merges its own items with these.
type SearchIndex struct {
	GeneratedAt   string         `json:"generatedAt"`
	Items         []SearchItem   `json:"items"`
	TopNetworkIDs []string       `json:"topNetworkIds"`
	GroupBonus    map[string]int `json:"groupBonus"`
	PerGroup      int            `json:"perGroup"`
	byID          map[string]*SearchItem
}

// GroupBonus and PerGroup are pi-palette.tsx's GROUP_BONUS and PER_GROUP,
// with its Sections and Guides groups, whose items the website adds. The
// palette has no cap on the results beyond PerGroup.
var GroupBonus = map[string]int{"Networks": 6, "Pages": 5, "Sections": 3, "Guides": 2, "Targets": 2, "Actions": 1}

const (
	PerGroup = 5
	// topNetworks is the palette's "Top networks" before typing.
	topNetworks = 5
	// searchRanking is the ranking the palette's networks come from: the
	// residential networks (pi-data.ts MEASURED) of the 30-day leaderboard.
	searchRanking = "30d/residential"
)

type pageItem struct{ path, title, subtitle, icon, keywords string }

// pages are pi-palette.tsx PAGES, the Proxy Index pages the API serves; the
// editorial ones (alternatives, guides, changelog, contribute, disclosure)
// stay website-local.
var pages = []pageItem{
	{"/proxy-index", "Overview", "Rankings, market figures and the live dashboard", "gauge", "home index dashboard market leaderboard ranking rankings score best top"},
	{"/proxy-index/networks", "Networks", "Every network in the Index, filtered and sorted", "layout-grid", "providers directory list all reviews profiles"},
	{"/proxy-index/compare", "Compare", "Up to four networks side by side", "columns-3", "comparison side by side vs versus"},
	{"/proxy-index/overlap", "Pool overlap", "Which networks share the same IPs", "layers", "shared pool resellers reseller exclusive exclusivity ips duplicate"},
	{"/proxy-index/true-cost", "True cost", "The price of 1,000 successful requests", "coins", "price pricing per gb gigabyte success retries retry bill budget money cheap expensive"},
	{"/proxy-index/live-tape", "Live tape", "Test events as they land", "activity", "feed events realtime real time incidents stream json"},
	{"/proxy-index/methodology", "Methodology", "How every network is tested and scored", "flask-conical", "method how we test testing latency speed success rate score scoring index score weights benchmark p50 p99 pipeline reproduce limits"},
	{"/proxy-index/targets", "Targets", "Success rate on each test website", "crosshair", "websites sites success by website difficulty anti bot hard"},
}

func sp(s string) *string { return &s }

// actions are pi-palette.tsx's dataset and range actions. A range action's
// command is "range:<r>", which the website runs on a page with windowed
// figures; its href is the home, the first such page.
func actions() []SearchItem {
	items := []SearchItem{{ID: "act:dataset", Group: "Actions", Title: "Download the dataset", Subtitle: "Every figure in the Index, as JSON",
		Href: "/proxy-index/dataset.json", Icon: "download", Keywords: terms("download export json data dataset api"), Download: true}}
	titles := map[string]string{"24h": "Show the last 24 hours", "7d": "Show the last 7 days", "30d": "Show the last 30 days"}
	for _, r := range Ranges {
		items = append(items, SearchItem{ID: "act:range:" + r.ID, Group: "Actions", Title: titles[r.ID], Subtitle: "Measurement window for every figure",
			Href: "/proxy-index", Icon: "calendar-range", Keywords: terms("range window period time day days week month", r.ID), Command: sp("range:" + r.ID)})
	}
	return items
}

// terms is pi-palette.tsx terms(): the parts lowercased, every run of
// characters that are not letters or digits one space.
func terms(parts ...string) string {
	f := func(r rune) bool { return !unicode.IsLetter(r) && !unicode.IsNumber(r) }
	return strings.Join(strings.FieldsFunc(strings.ToLower(strings.Join(parts, " ")), f), " ")
}

// buildSearchIndex is the palette's networks (the residential ranking's
// rows), the Proxy Index pages, the card targets and the actions. Item text
// is English; the website translates it by item id.
func buildSearchIndex(networks []RankingRow, targets []model.TargetMeta, generatedAt string) *SearchIndex {
	idx := &SearchIndex{GeneratedAt: generatedAt, GroupBonus: GroupBonus, PerGroup: PerGroup, TopNetworkIDs: []string{}}
	items := []SearchItem{}
	for i, row := range networks {
		if i < topNetworks {
			idx.TopNetworkIDs = append(idx.TopNetworkIDs, "net:"+row.Slug)
		}
		items = append(items, networkItem(row))
	}
	for _, p := range pages {
		items = append(items, SearchItem{ID: "page:" + p.path, Group: "Pages", Title: p.title, Subtitle: p.subtitle, Href: p.path, Icon: p.icon, Keywords: terms(p.keywords)})
	}
	for _, t := range targets {
		if isCard(t) {
			items = append(items, targetItem(t))
		}
	}
	items = append(items, actions()...)
	idx.byID = map[string]*SearchItem{}
	for i := range items {
		it := &items[i]
		it.lowTitle, it.lowSubtitle, it.lowKeywords = strings.ToLower(it.Title), strings.ToLower(it.Subtitle), strings.ToLower(it.Keywords)
		idx.byID[it.ID] = it
	}
	idx.Items = items
	return idx
}

// networkItem is a palette network: its tagline, and the meta "score · #rank"
// with the rank of its standing (W2), or "Unranked". The website draws a
// monogram, so it has no icon.
func networkItem(row RankingRow) SearchItem {
	score := jsnum.NA // format.ts fixed()
	if row.Score.OK() {
		score = jsnum.ToFixed(row.Score.F(), 1)
	}
	rank := "Unranked"
	if row.Standing.Rank.OK() {
		rank = "#" + jsnum.ToFixed(row.Standing.Rank.F(), 0)
	}
	return SearchItem{ID: "net:" + row.Slug, Group: "Networks", Title: row.Name, Subtitle: model.SV(row.Tagline),
		Href: "/proxy-index/networks/" + row.Slug, Meta: sp(score + " · " + rank), Keywords: terms(row.Slug, row.Name)}
}

// targetItem is a card target: its catalog summary, else its pass condition
// (the catalog has no summary today), linking to its card on the targets page.
func targetItem(t model.TargetMeta) SearchItem {
	sub := model.SV(t.PassCondition)
	return SearchItem{ID: "target:" + t.ID, Group: "Targets", Title: model.SV(t.Label), Subtitle: sub,
		Href: "/proxy-index/targets#" + t.ID, Icon: "crosshair",
		Keywords: terms(model.SV(t.Label), model.SV(t.Category), sub, t.ID, "target website site")}
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

func (it *SearchItem) score(tokens []string) int {
	total := 0
	for _, t := range tokens {
		at := runeIndex(it.lowTitle, t)
		switch {
		case at == 0:
			total += 120
		case at > 0:
			total += 90 - min(at, 40)
		case startsWord(it.lowKeywords, t):
			total += 55
		case strings.Contains(it.lowSubtitle, t):
			total += 45
		case looseMatch([]rune(t), []rune(it.lowTitle)):
			total += 18
		default:
			return 0
		}
	}
	return total + GroupBonus[it.Group]
}

// SearchSection is one group of results.
type SearchSection struct {
	Group string       `json:"group"`
	Items []SearchItem `json:"items"`
}

// SearchResult is /v1/search.
type SearchResult struct {
	Query    string          `json:"query"`
	Tokens   []string        `json:"tokens"`
	Total    int             `json:"total"`
	Sections []SearchSection `json:"sections"`
}

// Tokens normalises a query the way the palette does.
func Tokens(q string) []string {
	return strings.Fields(strings.ToLower(q))
}

// Search ranks the index like pi-palette.tsx. An empty query returns the
// palette's "Top networks"; its "Recent" and "Jump to" are website-local.
func (idx *SearchIndex) Search(q string) SearchResult {
	tokens := Tokens(q)
	res := SearchResult{Query: q, Tokens: tokens, Sections: []SearchSection{}}
	if len(tokens) == 0 {
		if top := idx.itemsByID(idx.TopNetworkIDs); len(top) > 0 {
			res.Sections, res.Total = []SearchSection{{Group: "Top networks", Items: top}}, len(top)
		}
	} else {
		res.Sections, res.Total = groupHits(idx.rankedHits(tokens))
	}
	return res
}

// itemsByID are the items of ids in order, skipping ids not in the index.
func (idx *SearchIndex) itemsByID(ids []string) []SearchItem {
	items := []SearchItem{}
	for _, id := range ids {
		if it, ok := idx.byID[id]; ok {
			items = append(items, *it)
		}
	}
	return items
}

type searchHit struct {
	it    *SearchItem
	value int
}

// rankedHits are the items matching every token, best first; ties keep index order.
func (idx *SearchIndex) rankedHits(tokens []string) []searchHit {
	hits := []searchHit{}
	for i := range idx.Items {
		if v := idx.Items[i].score(tokens); v > 0 {
			hits = append(hits, searchHit{&idx.Items[i], v})
		}
	}
	sort.SliceStable(hits, func(i, j int) bool { return hits[i].value > hits[j].value })
	return hits
}

// groupHits takes at most PerGroup hits of each group into sections in the
// order their groups first appear, and returns their count.
func groupHits(hits []searchHit) ([]SearchSection, int) {
	groups := []string{}
	byGroup := map[string][]SearchItem{}
	taken := 0
	for _, h := range hits {
		g := h.it.Group
		if len(byGroup[g]) >= PerGroup {
			continue
		}
		if _, ok := byGroup[g]; !ok {
			groups = append(groups, g)
		}
		byGroup[g] = append(byGroup[g], *h.it)
		taken++
	}
	sections := []SearchSection{}
	for _, g := range groups {
		sections = append(sections, SearchSection{Group: g, Items: byGroup[g]})
	}
	return sections, taken
}
