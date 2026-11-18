package build

import (
	"context"
	"fmt"
	"maps"
	"net/http"
	"net/url"
	"regexp"
	"slices"
	"sort"
	"strings"

	"github.com/shifter-io/atlas/api/internal/model"
	"github.com/shifter-io/atlas/api/internal/snap"
	"github.com/shifter-io/atlas/api/internal/store"
)

// PageAnswer is one family of page answers added from Phase 3
// (api/answers/README.md). Its own file registers it from an init():
// the build calls Build once per build, the server renders and compresses
// every body with the other answers and installs Route, whose handler serves
// the body stored under Key(r).
type PageAnswer struct {
	// Name is the family: every stored path is "/v1/<Name>" or under
	// "/v1/<Name>/", and its golden hashes are answers-v4-scale/<Name>.json.
	Name string
	// Build is every body of one build by its stored path. It reads only the
	// Context, never the database; an error leaves the family out of the
	// build, and /healthz lists its samples as unserved.
	Build func(*Context) (map[string]any, error)
	// Route is the mux pattern, such as "GET /v1/targets".
	Route string
	// Key validates the request and gives the stored path it reads; an
	// error (an APIError) answers 400 with its code and parameters.
	Key func(*http.Request) (string, error)
	// Samples are request URLs (path and query) for the tests, /healthz and
	// the bench; each must answer 200.
	Samples []string
	// InDataset adds the bodies to the dataset file's pages (DATASET-PAGES-2),
	// each under Request(path); with Cut, the cut of every held path.
	InDataset bool
	// Request, needed with InDataset, is the request URL that serves the
	// body stored under path, every parameter spelled out; "" leaves the
	// body out of the dataset file.
	Request func(path string) string
	// Cut, when set, cuts each body on request instead of rendering every
	// one at build: Build's values are held (Pages.Held), and a request is
	// answered with Cut(held, Key(r)), 404 when not found, rendered and kept
	// in the version's page LRU as the place pages are. For answers with
	// many filtered pages.
	Cut func(held map[string]any, path string) (body any, found bool)
	// Root, when set, is the stored paths' root in place of "/v1/<Name>",
	// for an answer under a reserved family's path (/v1/events/stats).
	Root string
	// Events makes Build run on every events refresh (WithEvents) too, with
	// the new events and the last build's GeneratedAt and Pages (all of the
	// Context it may read). It needs Cut: the values are held in
	// Result.Events.Held, and each body cut is kept in the version's events
	// LRU, which every refresh replaces.
	Events bool
}

// root is the stored paths' root: Root, else "/v1/<Name>".
func (a PageAnswer) root() string {
	if a.Root != "" {
		return a.Root
	}
	return "/v1/" + a.Name
}

// Owns reports whether path is one of a's stored paths.
func (a PageAnswer) Owns(path string) bool {
	return path == a.root() || strings.HasPrefix(path, a.root()+"/")
}

// Context is what a build already holds, handed to every registered answer.
type Context struct {
	GeneratedAt string
	Snapshot    *snap.Snapshot // nil when nothing is published
	Entries     []*entry       // every network, in catalog order
	Stats       map[string]map[string]*WindowStats
	Details     map[string]*ProviderDetail // by slug
	// Dataset is the dataset (its Targets, providers and measured market).
	Dataset *model.Dataset
	Events  *EventWindow
	// Pages is the answers built before the registered ones: the rankings
	// and the network answers (the all-networks one included).
	Pages *Pages
	// Inputs is the rows the build read: the site catalog (Providers,
	// Pricing, Plans, Scopes, Profiles, ...) and the framework's. Its
	// snapshot's network lists read the database: an answer never calls them.
	Inputs *store.Inputs
}

// reservedAnswers are the families of the answers served before the registry.
var reservedAnswers = map[string]bool{"rankings": true, "networks": true, "charts": true, "countries": true, "places": true,
	"coverage": true, "search": true, "events": true, "dataset": true, "version": true, "files": true}

var answerName = regexp.MustCompile(`^[a-z][a-z0-9-]{0,40}$`)

var answers = map[string]PageAnswer{}

// RegisterAnswer adds a page answer family. Like http.Handle, it panics on a
// programming error (a bad, reserved or repeated name, a missing function or
// route); call it from an init() only.
func RegisterAnswer(a PageAnswer) {
	switch {
	case !answerName.MatchString(a.Name) || reservedAnswers[a.Name]:
		panic(fmt.Sprintf("build: answer name %q is invalid or reserved", a.Name))
	case answers[a.Name].Name != "":
		panic(fmt.Sprintf("build: answer %q registered twice", a.Name))
	case a.Build == nil || a.Key == nil || !strings.HasPrefix(a.Route, "GET /v1/") || a.InDataset && a.Request == nil:
		panic(fmt.Sprintf("build: answer %q needs Build, Key and a GET /v1/ route, and Request with InDataset", a.Name))
	case a.Root != "" && !strings.HasPrefix(a.Root, "/v1/") || a.Events && a.Cut == nil:
		panic(fmt.Sprintf("build: answer %q needs a /v1/ Root, and Cut with Events", a.Name))
	}
	answers[a.Name] = a
}

// Answers is every registered answer, by name.
func Answers() []PageAnswer {
	out := make([]PageAnswer, 0, len(answers))
	for _, a := range answers {
		out = append(out, a)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out
}

// buildRegistered fills pg.Registered (pg.Held for an answer with Cut) from
// every registered answer. A failed answer, or one with a path outside its
// family, is left out and noted in pg.Unbuilt.
func (pg *Pages) buildRegistered(c *Context) {
	pg.buildFrom(c, Answers())
}

func (pg *Pages) buildFrom(c *Context, all []PageAnswer) {
	pg.Registered, pg.Held, pg.Unbuilt = map[string]any{}, map[string]map[string]any{}, map[string]string{}
	for _, a := range all {
		if a.Events {
			continue // EventWindow.buildLive
		}
		bodies, err := a.built(c)
		if err != nil {
			pg.Unbuilt[a.Name] = err.Error()
			continue
		}
		if a.Cut != nil {
			pg.Held[a.Name] = bodies
			continue
		}
		for path, body := range bodies {
			pg.Registered[path] = body
		}
	}
}

// built is a.Build(c), an error when a path is outside a's family.
func (a PageAnswer) built(c *Context) (map[string]any, error) {
	bodies, err := a.Build(c)
	for path := range bodies {
		if err == nil && !a.Owns(path) {
			err = fmt.Errorf("stored path %s is outside %s", path, a.root())
		}
	}
	return bodies, err
}

// buildLive fills w.Held with the bodies of every registered answer with
// Events, built from c with w as its Events; c is kept, with only the fields
// such an answer reads, for the next WithEvents. A failed answer is left out
// and noted in w.Unbuilt.
func (w *EventWindow) buildLive(c *Context) {
	w.live = &Context{GeneratedAt: c.GeneratedAt, Pages: c.Pages, Events: w}
	w.Held, w.Unbuilt = map[string]map[string]any{}, map[string]string{}
	for _, a := range Answers() {
		if !a.Events {
			continue
		}
		bodies, err := a.built(w.live)
		if err != nil {
			w.Unbuilt[a.Name] = err.Error()
			continue
		}
		w.Held[a.Name] = bodies
	}
}

// RangeIDs are the ranges a request may ask for.
var RangeIDs = []string{"24h", "7d", "30d"}

var (
	rangeIDs    = setOf(RangeIDs...)
	slugPattern = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{0,62}$`)
	kindPattern = regexp.MustCompile(`^[a-z][a-z0-9_]{0,40}$`)
)

// queryKey is the context key of a request's parsed query (WithQuery).
type queryKey struct{}

// WithQuery is r carrying q, its query already parsed (the server's request
// guard parses each query once), which RequestQuery then returns.
func WithQuery(r *http.Request, q url.Values) *http.Request {
	return r.WithContext(context.WithValue(r.Context(), queryKey{}, q))
}

// RequestQuery is r's parsed query: the one WithQuery gave it, else
// r.URL.Query(). The values are shared: read them, never change them.
func RequestQuery(r *http.Request) url.Values {
	if q, ok := r.Context().Value(queryKey{}).(url.Values); ok {
		return q
	}
	return r.URL.Query()
}

// Query is r's query when it has no parameter outside allowed.
func Query(r *http.Request, allowed ...string) (url.Values, error) {
	q := RequestQuery(r)
	for _, k := range slices.Sorted(maps.Keys(q)) {
		ok := false
		for _, a := range allowed {
			ok = ok || k == a
		}
		if !ok {
			return nil, UnknownParameter(k)
		}
	}
	return q, nil
}

// QueryRange is q's range: 24h, 7d or 30d (the default).
func QueryRange(q url.Values) (string, error) {
	r := q.Get("range")
	if r == "" {
		return "30d", nil
	}
	if !rangeIDs[r] {
		return "", InvalidValue("range", r, RangeIDs...)
	}
	return r, nil
}

// QueryKind is q's kind, or def when it has none.
func QueryKind(q url.Values, def string) (string, error) {
	k := q.Get("kind")
	if k == "" {
		return def, nil
	}
	if !kindPattern.MatchString(k) {
		return "", InvalidFormat("kind", k, FormatKind)
	}
	return k, nil
}

// queryRequest is root with the segments of path under it as the query
// parameters names, in order: "/v1/home/7d" under "/v1/home" with "range"
// is "/v1/home?range=7d". A path with more segments than names is "".
func queryRequest(path, root string, names ...string) string {
	rest := strings.TrimPrefix(strings.TrimPrefix(path, root), "/")
	if rest == "" {
		return root
	}
	segs := strings.Split(rest, "/")
	if len(segs) > len(names) {
		return ""
	}
	for i, s := range segs {
		segs[i] = names[i] + "=" + s
	}
	return root + "?" + strings.Join(segs, "&")
}

// MaxPage is the highest page a paged answer's request may ask for.
const MaxPage = 100000

// QueryPage is q's page, 1 by default.
func QueryPage(q url.Values) (int, error) {
	p := q.Get("page")
	if p == "" {
		return 1, nil
	}
	return IntParam("page", p, 1, MaxPage)
}

// PathSlug is the route's {slug}.
func PathSlug(r *http.Request) (string, error) {
	s := r.PathValue("slug")
	if !slugPattern.MatchString(s) {
		return "", InvalidFormat("slug", s, FormatSlug)
	}
	return s, nil
}
