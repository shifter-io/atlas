package build

import (
	"os"
	"path/filepath"
	"sort"
	"strings"
	"unicode/utf8"

	"github.com/Zeus-Development/proxy-index-website/api/internal/jsnum"
	"github.com/Zeus-Development/proxy-index-website/api/internal/model"
	"gopkg.in/yaml.v3"
)

// SearchItem is src/lib/search.ts SearchItem; every field is always written.
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

// SearchIndex is /v1/search-index.
type SearchIndex struct {
	GeneratedAt      string         `json:"generatedAt"`
	Items            []SearchItem   `json:"items"`
	StartingPointIDs []string       `json:"startingPointIds"`
	TopNetworkIDs    []string       `json:"topNetworkIds"`
	GroupBonus       map[string]int `json:"groupBonus"`
	MaxResults       int            `json:"maxResults"`
	PerGroup         int            `json:"perGroup"`
	byID             map[string]*SearchItem
}

// GroupBonus, MaxResults and PerGroup are CommandPalette.astro's constants;
// Countries is new in the API index.
var GroupBonus = map[string]int{"Networks": 6, "Guides": 4, "Pages": 3, "Targets": 2, "Countries": 2, "Actions": 1}

const (
	MaxResults = 16
	PerGroup   = 5
)

var startingPointIDs = []string{"page:/networks", "page:/compare", "page:/overlap", "page:/pricing", "action:dataset"}

type navItem struct{ href, label, icon, match string }
type navGroup struct {
	name  string
	items []navItem
}

// navGroups is src/lib/nav.ts NAV_GROUPS with the English labels of src/i18n/ui/en.ts.
var navGroups = []navGroup{
	{"Measure", []navItem{{"/", "Overview", "grid", "overview"}, {"/networks", "Networks", "server", "networks"}, {"/compare", "Compare", "scale", "compare"}, {"/overlap", "Pool overlap", "percent", "overlap"}, {"/pricing", "True cost", "tag", "pricing"}}},
	{"Method", []navItem{{"/targets", "Targets", "flag", "targets"}, {"/guides", "Guides", "beaker", "guides"}, {"/methodology", "Methodology", "beaker", "methodology"}}},
	{"Record", []navItem{{"/changelog", "What changed", "clock", "changelog"}, {"/contribute", "Contribute", "copy", "contribute"}, {"/disclosure", "Disclosure", "shield", "disclosure"}}},
}

func pageBlurbs(targets int) map[string]string {
	return map[string]string{
		"overview":    "Fleet-wide headline numbers and the live request tape",
		"networks":    "Every measured network, ranked by MILF Score",
		"compare":     "Put two networks side by side on the same run",
		"overlap":     "How much of each pool turns up inside a rival network",
		"pricing":     "Dollars per 1,000 successful requests, not sticker price",
		"targets":     "The " + jsnum.Locale(float64(targets)) + " endpoints every network is tested against",
		"guides":      "How to buy, test and read proxy measurements",
		"methodology": "Scoring formula, request schedule and window",
		"changelog":   "What moved since the previous run",
		"contribute":  "Submit a collector run of your own",
		"disclosure":  "Funding, conflicts and why ranking cannot be bought",
	}
}

func sp(s string) *string { return &s }

var actions = []SearchItem{
	{ID: "action:dataset", Group: "Actions", Title: "Download the raw dataset", Subtitle: "Every figure on this site, as one JSON file", Href: "/data/design-preview.json", Icon: "download", Keywords: "json export raw data file api schema", Download: true, Command: sp("dataset")},
	{ID: "action:apply", Group: "Actions", Title: "List your proxy network", Subtitle: "Apply to be measured and published", Href: "#", Icon: "plus", Keywords: "submit apply listing add provider vendor join measure register signup sign up", Command: sp("apply")},
	{ID: "action:theme", Group: "Actions", Title: "Toggle dark / light theme", Subtitle: "Switch the colour scheme for this browser", Href: "#", Icon: "moon", Keywords: "dark light appearance colour color scheme contrast", Command: sp("theme")},
	{ID: "action:copy", Group: "Actions", Title: "Copy link to this page", Subtitle: "Puts the current URL on the clipboard", Href: "#", Icon: "copy", Keywords: "share url clipboard permalink", Command: sp("copy")},
}

// Content is the frontmatter of src/content guides and reviews.
type Content struct {
	Guides  []Guide
	Reviews []EditorialReview
}

type Guide struct {
	ID             string
	Title          string   `yaml:"title"`
	Description    string   `yaml:"description"`
	Category       string   `yaml:"category"`
	ReadingMinutes int      `yaml:"readingMinutes"`
	Order          *int     `yaml:"order"`
	Takeaways      []string `yaml:"takeaways"`
}

type EditorialReview struct {
	ID             string
	Provider       string `yaml:"provider"`
	Product        string `yaml:"product"`
	Title          string `yaml:"title"`
	BottomLine     string `yaml:"bottomLine"`
	Verdict        string `yaml:"verdict"`
	ReadingMinutes int    `yaml:"readingMinutes"`
}

func frontmatter(path string, v any) error {
	body, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	text := strings.ReplaceAll(string(body), "\r\n", "\n")
	if !strings.HasPrefix(text, "---\n") {
		return nil
	}
	end := strings.Index(text[4:], "\n---")
	if end < 0 {
		return nil
	}
	return yaml.Unmarshal([]byte(text[4:4+end]), v)
}

// ReadContent loads guide and review frontmatter from a src/content directory.
// A missing directory yields empty content.
func ReadContent(dir string) (*Content, error) {
	c := &Content{}
	if dir == "" {
		return c, nil
	}
	guides, _ := filepath.Glob(filepath.Join(dir, "guides", "*.md"))
	sort.Strings(guides)
	for _, f := range guides {
		var g Guide
		if err := frontmatter(f, &g); err != nil {
			return nil, err
		}
		g.ID = strings.TrimSuffix(filepath.Base(f), ".md")
		c.Guides = append(c.Guides, g)
	}
	reviews, _ := filepath.Glob(filepath.Join(dir, "reviews", "*.md"))
	sort.Strings(reviews)
	for _, f := range reviews {
		var r EditorialReview
		if err := frontmatter(f, &r); err != nil {
			return nil, err
		}
		r.ID = strings.TrimSuffix(filepath.Base(f), ".md")
		c.Reviews = append(c.Reviews, r)
	}
	return c, nil
}

func dash(s string) string  { return strings.ReplaceAll(s, "-", " ") }
func under(s string) string { return strings.ReplaceAll(s, "_", " ") }

// buildSearchIndex ports search.ts buildSearchIndex (English paths), plus a
// Countries group built from where measured exits were seen.
func buildSearchIndex(leaderboard []*model.Provider, targets []model.TargetMeta, content *Content, generatedAt string) *SearchIndex {
	idx := &SearchIndex{GeneratedAt: generatedAt, StartingPointIDs: startingPointIDs, GroupBonus: GroupBonus, MaxResults: MaxResults, PerGroup: PerGroup, TopNetworkIDs: []string{}}
	items := []SearchItem{}
	listed := map[string]bool{}
	for i, p := range leaderboard {
		listed[p.Slug] = true
		if i < 5 {
			idx.TopNetworkIDs = append(idx.TopNetworkIDs, "network:"+p.Slug)
		}
		// The network's standing (every-request.ts kindStanding): a measured
		// network alone in its kind with a ready score is "#1 of 1 in its kind".
		rank := p.Score.KindStanding()
		composite := jsnum.NA // format.ts fixed()
		if p.Score.Composite.OK() {
			composite = jsnum.ToFixed(p.Score.Composite.F(), 1)
		}
		hq := jsnum.NA
		if p.HQ != nil {
			hq = *p.HQ
		}
		grade := jsnum.NA
		if p.Score.Grade != nil {
			grade = *p.Score.Grade
		}
		kw := []string{dash(p.Slug), model.SV(p.Tagline), "grade " + grade}
		for _, k := range p.Kinds {
			kw = append(kw, model.KindLabels[k])
		}
		kw = append(kw, p.BestFor...)
		for i, g := range p.Geo {
			if i >= 8 {
				break
			}
			kw = append(kw, model.SV(g.Country))
		}
		items = append(items, SearchItem{ID: "network:" + p.Slug, Group: "Networks", Title: p.Name,
			Subtitle: model.KindLabels[p.PrimaryKind] + " · " + model.ProductLabels[p.MeasuredProduct] + " · " + hq,
			Href:     "/networks/" + p.Slug, Icon: "server", Meta: sp(rank + " · " + composite), Keywords: strings.Join(kw, " ")})
	}
	if content != nil {
		guides := append([]Guide{}, content.Guides...)
		order := func(g Guide) int {
			if g.Order == nil {
				return 50
			}
			return *g.Order
		}
		sort.SliceStable(guides, func(i, j int) bool { return order(guides[i]) < order(guides[j]) })
		for _, g := range guides {
			items = append(items, SearchItem{ID: "guide:" + g.ID, Group: "Guides", Title: g.Title,
				Subtitle: g.Category + " · " + jsnum.Locale(float64(g.ReadingMinutes)) + " min read", Href: "/guides/" + g.ID, Icon: "beaker",
				Keywords: strings.Join(append([]string{dash(g.ID), g.Description}, g.Takeaways...), " ")})
		}
		for _, r := range content.Reviews {
			if !listed[r.Provider] {
				continue
			}
			items = append(items, SearchItem{ID: "review:" + r.ID, Group: "Guides", Title: r.Title,
				Subtitle: "Network review · " + jsnum.Locale(float64(r.ReadingMinutes)) + " min · opens on the network page",
				Href:     "/networks/" + r.Provider + "#analysis", Icon: "shield",
				Keywords: strings.Join([]string{dash(r.ID), dash(r.Provider), r.BottomLine, r.Verdict, r.Product}, " ")})
		}
	}
	blurbs := pageBlurbs(len(targets))
	for _, g := range navGroups {
		for _, it := range g.items {
			sub, ok := blurbs[it.match]
			if !ok {
				sub = g.name
			}
			items = append(items, SearchItem{ID: "page:" + it.href, Group: "Pages", Title: it.label, Subtitle: sub, Href: it.href, Icon: it.icon, Keywords: g.name + " " + it.match})
		}
	}
	for _, t := range targets {
		meta := sp("difficulty " + jsnum.NA + "/5")
		if t.Difficulty.OK() {
			meta = sp("difficulty " + jsnum.ToFixed(t.Difficulty.F(), 0) + "/5")
		}
		cat, pass := jsnum.NA, jsnum.NA
		if t.Category != nil {
			cat = *t.Category
		}
		if t.PassCondition != nil {
			pass = *t.PassCondition
		}
		items = append(items, SearchItem{ID: "target:" + t.ID, Group: "Targets", Title: model.SV(t.Label), Subtitle: cat + " · " + pass,
			Href: "/targets", Icon: "flag", Meta: meta, Keywords: under(t.ID) + " test target endpoint"})
	}
	// Countries: every country with exits in at least one network.
	type country struct {
		code, name string
		networks   []string
		ips        float64
	}
	countries := map[string]*country{}
	for _, p := range leaderboard {
		for _, g := range p.Geo {
			c := countries[g.CountryCode]
			if c == nil {
				c = &country{code: g.CountryCode, name: model.SV(g.Country)}
				countries[g.CountryCode] = c
			}
			c.networks = append(c.networks, p.Name)
			if g.Ips.OK() {
				c.ips += g.Ips.F()
			}
		}
	}
	codes := make([]string, 0, len(countries))
	for code := range countries {
		codes = append(codes, code)
	}
	sort.Strings(codes)
	for _, code := range codes {
		c := countries[code]
		n := float64(len(c.networks))
		word := "networks"
		if n == 1 {
			word = "network"
		}
		items = append(items, SearchItem{ID: "country:" + code, Group: "Countries", Title: c.name,
			Subtitle: jsnum.Locale(n) + " " + word + " with exits here · " + jsnum.Locale(c.ips) + " exit IPs",
			Href:     "/networks?country=" + code, Icon: "globe", Meta: sp(code),
			Keywords: strings.ToLower(code) + " country geo exits " + strings.Join(c.networks, " ")})
	}
	items = append(items, actions...)
	idx.byID = map[string]*SearchItem{}
	for i := range items {
		it := &items[i]
		it.lowTitle, it.lowSubtitle, it.lowKeywords = strings.ToLower(it.Title), strings.ToLower(it.Subtitle), strings.ToLower(it.Keywords)
		idx.byID[it.ID] = it
	}
	idx.Items = items
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
			return at-start < len(needle)*3
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
		case strings.Contains(it.lowSubtitle, t):
			total += 45
		case startsWord(it.lowKeywords, t):
			total += 30
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

// Search ranks the index like CommandPalette.astro sectionsFor. An empty
// query returns the palette's default sections (starting points, top networks).
func (idx *SearchIndex) Search(q string) SearchResult {
	tokens := Tokens(q)
	res := SearchResult{Query: q, Tokens: tokens, Sections: []SearchSection{}}
	if len(tokens) == 0 {
		for _, s := range []struct {
			label string
			ids   []string
		}{{"Jump to", idx.StartingPointIDs}, {"Top networks", idx.TopNetworkIDs}} {
			sec := SearchSection{Group: s.label, Items: []SearchItem{}}
			for _, id := range s.ids {
				if it, ok := idx.byID[id]; ok {
					sec.Items = append(sec.Items, *it)
				}
			}
			if len(sec.Items) > 0 {
				res.Sections = append(res.Sections, sec)
				res.Total += len(sec.Items)
			}
		}
		return res
	}
	type hit struct {
		it    *SearchItem
		value int
	}
	hits := []hit{}
	for i := range idx.Items {
		if v := idx.Items[i].score(tokens); v > 0 {
			hits = append(hits, hit{&idx.Items[i], v})
		}
	}
	sort.SliceStable(hits, func(i, j int) bool { return hits[i].value > hits[j].value })
	groups := []string{}
	byGroup := map[string][]SearchItem{}
	taken := 0
	for _, h := range hits {
		if taken >= MaxResults {
			break
		}
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
	for _, g := range groups {
		res.Sections = append(res.Sections, SearchSection{Group: g, Items: byGroup[g]})
	}
	res.Total = taken
	return res
}
