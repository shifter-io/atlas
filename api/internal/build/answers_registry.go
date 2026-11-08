package build

import (
	"fmt"
	"net/http"
	"net/url"
	"regexp"
	"sort"
	"strconv"
	"strings"

	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/Zeus-Development/proxy-index/api/internal/snap"
	"github.com/Zeus-Development/proxy-index/api/internal/store"
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
	// error answers 400 with its text.
	Key func(*http.Request) (string, error)
	// Samples are request URLs (path and query) for the tests, /healthz and
	// the bench; each must answer 200.
	Samples []string
	// InDataset adds the bodies to the dataset file's pages (DATASET-PAGES-2).
	InDataset bool
	// Cut, when set, cuts each body on request instead of rendering every
	// one at build: Build's values are held (Pages.Held), and a request is
	// answered with Cut(held, Key(r)), 404 when not found, rendered and kept
	// in the version's page LRU as the place pages are. For answers with
	// many filtered pages; not with InDataset.
	Cut func(held map[string]any, path string) (body any, found bool)
}

// Owns reports whether path is one of a's stored paths.
func (a PageAnswer) Owns(path string) bool {
	return path == "/v1/"+a.Name || strings.HasPrefix(path, "/v1/"+a.Name+"/")
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
	// Inputs is the rows the build read: the site catalog (Providers,
	// Pricing, Plans, Scopes, Profiles, ...) and the framework's. Its
	// snapshot's network lists read the database: an answer never calls them.
	Inputs *store.Inputs
}

// reservedAnswers are the families of the answers served before the registry.
var reservedAnswers = map[string]bool{"rankings": true, "networks": true, "charts": true, "countries": true, "places": true,
	"coverage": true, "search": true, "search-index": true, "events": true, "dataset": true, "version": true, "files": true}

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
	case a.Build == nil || a.Key == nil || !strings.HasPrefix(a.Route, "GET /v1/") || a.Cut != nil && a.InDataset:
		panic(fmt.Sprintf("build: answer %q needs Build, Key and a GET /v1/ route, and no Cut with InDataset", a.Name))
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
		bodies, err := a.Build(c)
		for path := range bodies {
			if err == nil && !a.Owns(path) {
				err = fmt.Errorf("stored path %s is outside /v1/%s", path, a.Name)
			}
		}
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

// BadRequest is a query a Key refuses.
type BadRequest string

func (e BadRequest) Error() string { return string(e) }

var (
	rangeIDs    = map[string]bool{"24h": true, "7d": true, "30d": true}
	slugPattern = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{0,62}$`)
	kindPattern = regexp.MustCompile(`^[a-z][a-z0-9_]{0,40}$`)
)

// Query is r's query when it has no parameter outside allowed.
func Query(r *http.Request, allowed ...string) (url.Values, error) {
	q := r.URL.Query()
	for k := range q {
		ok := false
		for _, a := range allowed {
			ok = ok || k == a
		}
		if !ok {
			return nil, BadRequest("unknown query parameter " + k)
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
		return "", BadRequest("range must be 24h, 7d or 30d")
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
		return "", BadRequest("invalid kind")
	}
	return k, nil
}

// MaxPage is the highest page a paged answer's request may ask for.
const MaxPage = 100000

// QueryPage is q's page, 1 by default.
func QueryPage(q url.Values) (int, error) {
	p := q.Get("page")
	if p == "" {
		return 1, nil
	}
	n, err := strconv.Atoi(p)
	if err != nil || n < 1 || n > MaxPage {
		return 0, BadRequest("page must be a positive number")
	}
	return n, nil
}

// PathSlug is the route's {slug}.
func PathSlug(r *http.Request) (string, error) {
	s := r.PathValue("slug")
	if !slugPattern.MatchString(s) {
		return "", BadRequest("invalid network")
	}
	return s, nil
}
