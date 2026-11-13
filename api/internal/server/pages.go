package server

import (
	"maps"
	"net/http"
	"net/url"
	"regexp"
	"slices"
	"strconv"
	"strings"

	"github.com/Zeus-Development/atlas/api/internal/build"
	"github.com/Zeus-Development/atlas/api/internal/model"
)

// PagesLRU bounds the rendered pages of long place lists kept per version.
const PagesLRU = 1024

// maxPlaceKey is the longest place key a request may name, in bytes.
const maxPlaceKey = 400

var (
	slugPattern    = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{0,62}$`)
	countryPattern = regexp.MustCompile(`^[A-Za-z]{2}$`)
)

// pageAnswers is every precomputed per-page body of a build by its path,
// the registered answers' (registered.go) among them.
// A network's answer for each category is stored under /v1/networks/{slug}/{kind};
// /v1/networks/{slug} without ?kind= serves the headline one.
func pageAnswers(pg *build.Pages) map[string]any {
	out := map[string]any{"/v1/networks": pg.Networks, "/v1/countries": pg.Countries}
	for key, ans := range pg.Network {
		out["/v1/networks/"+key] = ans
	}
	for cc, ans := range pg.Country {
		out["/v1/countries/"+cc] = ans
	}
	for r, ans := range pg.Rankings {
		out["/v1/rankings/"+r] = ans
	}
	for key, ans := range pg.RankingsByKind {
		out["/v1/rankings/"+key] = ans
	}
	for key, ans := range pg.Charts {
		out["/v1/charts/"+key] = ans
	}
	for path, ans := range pg.Registered {
		out[path] = ans
	}
	return out
}

func (s *Server) pageRoutes(mux *http.ServeMux) {
	mux.HandleFunc("GET /v1/networks", func(w http.ResponseWriter, r *http.Request) {
		if onlyParams(w, r.URL.Query()) {
			s.serveStatic(w, r, "/v1/networks")
		}
	})
	mux.HandleFunc("GET /v1/countries", func(w http.ResponseWriter, r *http.Request) {
		if onlyParams(w, r.URL.Query()) {
			s.serveStatic(w, r, "/v1/countries")
		}
	})
	mux.HandleFunc("GET /v1/rankings", func(w http.ResponseWriter, r *http.Request) {
		q := r.URL.Query()
		if !onlyParams(w, q, "range", "kind") {
			return
		}
		rangeID, err := build.QueryRange(q)
		if err != nil {
			badRequest(w, err)
			return
		}
		path, kind := "/v1/rankings/"+rangeID, q.Get("kind")
		if kind != "" {
			if !model.IsProxyKind(kind) {
				badRequest(w, build.InvalidValue("kind", kind, model.ProxyKinds...))
				return
			}
			path += "/" + kind
		}
		s.serveStatic(w, r, path)
	})
	mux.HandleFunc("GET /v1/networks/{slug}", s.serveNetwork)
	mux.HandleFunc("GET /v1/networks/{slug}/places", s.serveNetworkPlaces)
	mux.HandleFunc("GET /v1/networks/{slug}/coverage", s.serveNetworkPlaces)
	mux.HandleFunc("GET /v1/networks/{slug}/chart", s.serveNetworkChart)
	mux.HandleFunc("GET /v1/countries/{cc}", func(w http.ResponseWriter, r *http.Request) {
		cc := r.PathValue("cc")
		if !countryPattern.MatchString(cc) {
			badRequest(w, build.InvalidFormat("cc", cc, build.FormatCountry))
			return
		}
		if onlyParams(w, r.URL.Query()) {
			s.serveStatic(w, r, "/v1/countries/"+strings.ToUpper(cc))
		}
	})
	mux.HandleFunc("GET /v1/countries/{cc}/cities", s.serveCountryCities)
	mux.HandleFunc("GET /v1/countries/{cc}/regions", s.serveCountryCities)
	mux.HandleFunc("GET /v1/places/{dimension}/{key}", s.servePlace)
	s.installRegistered(mux)
}

// onlyParams answers 400 and returns false when the query has a parameter
// outside allowed.
func onlyParams(w http.ResponseWriter, q url.Values, allowed ...string) bool {
	for _, k := range slices.Sorted(maps.Keys(q)) {
		ok := false
		for _, a := range allowed {
			ok = ok || k == a
		}
		if !ok {
			badRequest(w, build.UnknownParameter(k))
			return false
		}
	}
	return true
}

// validSlug answers 400 and returns false when slug is no network slug.
func validSlug(w http.ResponseWriter, slug string) bool {
	if !slugPattern.MatchString(slug) {
		badRequest(w, build.InvalidFormat("slug", slug, build.FormatSlug))
		return false
	}
	return true
}

func (s *Server) serveNetwork(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	slug := r.PathValue("slug")
	if !onlyParams(w, q, "kind") || !validSlug(w, slug) {
		return
	}
	v := s.current.Load()
	if v == nil {
		s.unavailable(w)
		return
	}
	kind := q.Get("kind")
	if kind == "" {
		kind = v.Result.Pages.Primary[slug]
	}
	s.serveStatic(w, r, "/v1/networks/"+slug+"/"+kind)
}

// serveNetworkChart serves a network's histories for one category (default
// its headline one) and range (default 30d).
func (s *Server) serveNetworkChart(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	if !onlyParams(w, q, "kind", "range") {
		return
	}
	slug := r.PathValue("slug")
	if !validSlug(w, slug) {
		return
	}
	rangeID, err := build.QueryRange(q)
	if err != nil {
		badRequest(w, err)
		return
	}
	v := s.current.Load()
	if v == nil {
		s.unavailable(w)
		return
	}
	kind := q.Get("kind")
	if kind == "" {
		kind = v.Result.Pages.Primary[slug]
	}
	s.serveStatic(w, r, "/v1/charts/"+slug+"/"+kind+"/"+rangeID)
}

// listParams reads range, q and page.
func listParams(w http.ResponseWriter, q url.Values, extra ...string) (rangeID, query string, page int, ok bool) {
	if !onlyParams(w, q, append([]string{"range", "q", "page"}, extra...)...) {
		return "", "", 0, false
	}
	rangeID, err := build.QueryRange(q)
	if err == nil && len(q.Get("q")) > maxQuery {
		err = build.TooLong("q", maxQuery)
	}
	if err == nil {
		page, err = build.QueryPage(q)
	}
	if err != nil {
		badRequest(w, err)
		return "", "", 0, false
	}
	return rangeID, q.Get("q"), page, true
}

// serveNetworkPlaces serves a network's place lists and, for .../coverage,
// its Coverage answer; country= (a two-letter code, any case) keeps that
// country's rows.
func (s *Server) serveNetworkPlaces(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	coverage := strings.HasSuffix(r.URL.Path, "/coverage")
	extra := []string{"country", "kind", "dimension"}
	if coverage {
		extra = extra[:2]
	}
	rangeID, query, page, ok := listParams(w, q, extra...)
	if !ok {
		return
	}
	f := build.ListFilter{Query: query, Country: strings.ToUpper(q.Get("country"))}
	if q.Has("country") && !countryPattern.MatchString(f.Country) {
		badRequest(w, build.InvalidFormat("country", q.Get("country"), build.FormatCountry))
		return
	}
	slug := r.PathValue("slug")
	if !validSlug(w, slug) {
		return
	}
	dimension := q.Get("dimension")
	switch {
	case coverage:
		dimension = "coverage"
	case dimension == "coverage":
		notFound(w, r) // the Coverage answer is .../coverage, not a place list
		return
	case dimension == "":
		dimension = "country"
	}
	v := s.current.Load()
	if v == nil {
		s.unavailable(w)
		return
	}
	kind := q.Get("kind")
	if kind == "" {
		kind = v.Result.Pages.Primary[slug]
	}
	s.servePage(w, r, v, slug+"/"+kind, rangeID, dimension, f, page)
}

func (s *Server) serveCountryCities(w http.ResponseWriter, r *http.Request) {
	rangeID, query, page, ok := listParams(w, r.URL.Query())
	if !ok {
		return
	}
	cc := r.PathValue("cc")
	if !countryPattern.MatchString(cc) {
		badRequest(w, build.InvalidFormat("cc", cc, build.FormatCountry))
		return
	}
	v := s.current.Load()
	if v == nil {
		s.unavailable(w)
		return
	}
	dimension := "city"
	if strings.HasSuffix(r.URL.Path, "/regions") {
		dimension = "region"
	}
	s.servePage(w, r, v, "country/"+strings.ToUpper(cc), rangeID, dimension, build.ListFilter{Query: query}, page)
}

// servePlace serves a v4 build's region or city page (range defaults to
// 30d), keeping the rendered body in the version's page LRU.
func (s *Server) servePlace(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	if !onlyParams(w, q, "range") {
		return
	}
	dimension, key := r.PathValue("dimension"), r.PathValue("key")
	rangeID, err := build.QueryRange(q)
	switch {
	case dimension != "region" && dimension != "city":
		err = build.InvalidValue("dimension", dimension, "region", "city")
	case len(key) > maxPlaceKey:
		err = build.TooLong("key", maxPlaceKey)
	}
	if err != nil {
		badRequest(w, err)
		return
	}
	v := s.current.Load()
	if v == nil {
		s.unavailable(w)
		return
	}
	s.serveCut(w, r, v, strings.Join([]string{"place", dimension, key, rangeID}, "\x00"), func() (any, bool, error) {
		return v.Result.Pages.Place(r.Context(), v.Result.Dataset.GeneratedAt, dimension, key, rangeID)
	})
}

// servePage cuts one page from a list (a v4 build's first page without a
// filter is held by the build) and keeps the rendered body in the version's
// LRU.
func (s *Server) servePage(w http.ResponseWriter, r *http.Request, v *Version, scope, rangeID, dimension string, f build.ListFilter, page int) {
	key := strings.Join([]string{scope, rangeID, dimension, strconv.Itoa(page), strings.Join(build.Tokens(f.Query), " "), f.Country}, "\x00")
	s.serveCut(w, r, v, key, func() (any, bool, error) {
		at, pg := v.Result.Dataset.GeneratedAt, v.Result.Pages
		if dimension == "coverage" {
			return pg.Coverage(r.Context(), at, scope, rangeID, f, page)
		}
		return pg.List(r.Context(), at, scope, rangeID, dimension, f, page)
	})
}

// serveCut serves the body under key in the version's LRU, rendering and
// adding the answer cut gives when absent (404 when it has none, 500 when its
// rows could not be read).
func (s *Server) serveCut(w http.ResponseWriter, r *http.Request, v *Version, key string, cut func() (any, bool, error)) {
	s.serveCutIn(w, r, v, cutCache{v.pages, CacheData}, key, cut)
}

// cutCache is where serveCutIn keeps a cut body, and its Cache-Control.
type cutCache struct {
	lru     *LRU
	control string
}

// serveCutIn is serveCut keeping the body in c.lru with c.control.
func (s *Server) serveCutIn(w http.ResponseWriter, r *http.Request, v *Version, c cutCache, key string, cut func() (any, bool, error)) {
	body, ok := c.lru.Get(key)
	if !ok {
		res, found, err := cut()
		if err != nil {
			s.renderFailed(w, r, err)
			return
		}
		if !found {
			notFound(w, r)
			return
		}
		if body, err = Render(http.StatusOK, res, c.control, LevelFast); err != nil {
			s.renderFailed(w, r, err)
			return
		}
		c.lru.Add(key, body)
	}
	s.write(w, r, v, body)
}
