package server

import (
	"net/http"
	"net/url"
	"regexp"
	"strconv"
	"strings"

	"github.com/Zeus-Development/proxy-index/api/internal/build"
	"github.com/Zeus-Development/proxy-index/api/internal/model"
)

// PagesLRU bounds the rendered pages of long place lists kept per version.
const PagesLRU = 1024

// MaxPage is the highest page number a list request may ask for.
const MaxPage = 100000

var (
	slugPattern    = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{0,62}$`)
	countryPattern = regexp.MustCompile(`^[A-Za-z]{2}$`)
	rangeIDs       = map[string]bool{"24h": true, "7d": true, "30d": true}
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
		rangeID, kind := q.Get("range"), q.Get("kind")
		if rangeID == "" {
			rangeID = "30d"
		}
		if !rangeIDs[rangeID] {
			writeError(w, http.StatusBadRequest, "range must be 24h, 7d or 30d")
			return
		}
		path := "/v1/rankings/" + rangeID
		if kind != "" {
			if !model.IsProxyKind(kind) {
				writeError(w, http.StatusBadRequest, "kind must be residential, datacenter, mobile or isp")
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
			writeError(w, http.StatusBadRequest, "country must be a two-letter code")
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
	for k := range q {
		ok := false
		for _, a := range allowed {
			ok = ok || k == a
		}
		if !ok {
			writeError(w, http.StatusBadRequest, "unknown query parameter "+k)
			return false
		}
	}
	return true
}

func (s *Server) serveNetwork(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	slug := r.PathValue("slug")
	if !onlyParams(w, q, "kind") {
		return
	}
	if !slugPattern.MatchString(slug) {
		writeError(w, http.StatusBadRequest, "invalid network")
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
	slug, rangeID := r.PathValue("slug"), q.Get("range")
	if rangeID == "" {
		rangeID = "30d"
	}
	if !slugPattern.MatchString(slug) || !rangeIDs[rangeID] {
		writeError(w, http.StatusBadRequest, "invalid network or range (24h, 7d or 30d)")
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
	rangeID, query, page = q.Get("range"), q.Get("q"), 1
	if rangeID == "" {
		rangeID = "30d"
	}
	if !rangeIDs[rangeID] {
		writeError(w, http.StatusBadRequest, "range must be 24h, 7d or 30d")
		return "", "", 0, false
	}
	if len(query) > 200 {
		writeError(w, http.StatusBadRequest, "query too long")
		return "", "", 0, false
	}
	if p := q.Get("page"); p != "" {
		n, err := strconv.Atoi(p)
		if err != nil || n < 1 || n > MaxPage {
			writeError(w, http.StatusBadRequest, "page must be a positive number")
			return "", "", 0, false
		}
		page = n
	}
	return rangeID, query, page, true
}

// serveNetworkPlaces serves a network's place lists and, for .../coverage,
// its Coverage answer.
func (s *Server) serveNetworkPlaces(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	coverage := strings.HasSuffix(r.URL.Path, "/coverage")
	extra := []string{"kind", "dimension"}
	if coverage {
		extra = extra[:1]
	}
	rangeID, query, page, ok := listParams(w, q, extra...)
	if !ok {
		return
	}
	slug := r.PathValue("slug")
	if !slugPattern.MatchString(slug) {
		writeError(w, http.StatusBadRequest, "invalid network")
		return
	}
	dimension := q.Get("dimension")
	switch {
	case coverage:
		dimension = "coverage"
	case dimension == "coverage":
		writeError(w, http.StatusNotFound, "not found") // the Coverage answer is .../coverage, not a place list
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
	s.servePage(w, r, v, slug+"/"+kind, rangeID, dimension, query, page)
}

func (s *Server) serveCountryCities(w http.ResponseWriter, r *http.Request) {
	rangeID, query, page, ok := listParams(w, r.URL.Query())
	if !ok {
		return
	}
	cc := r.PathValue("cc")
	if !countryPattern.MatchString(cc) {
		writeError(w, http.StatusBadRequest, "country must be a two-letter code")
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
	s.servePage(w, r, v, "country/"+strings.ToUpper(cc), rangeID, dimension, query, page)
}

// servePlace serves a v4 build's region or city page (range defaults to
// 30d), keeping the rendered body in the version's page LRU.
func (s *Server) servePlace(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	if !onlyParams(w, q, "range") {
		return
	}
	dimension, key, rangeID := r.PathValue("dimension"), r.PathValue("key"), q.Get("range")
	if rangeID == "" {
		rangeID = "30d"
	}
	if dimension != "region" && dimension != "city" || !rangeIDs[rangeID] || len(key) > 400 {
		writeError(w, http.StatusBadRequest, "invalid place (region or city, a key, range 24h, 7d or 30d)")
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
// query is held by the build) and keeps the rendered body in the version's
// LRU.
func (s *Server) servePage(w http.ResponseWriter, r *http.Request, v *Version, scope, rangeID, dimension, query string, page int) {
	key := strings.Join([]string{scope, rangeID, dimension, strconv.Itoa(page), strings.Join(build.Tokens(query), " ")}, "\x00")
	s.serveCut(w, r, v, key, func() (any, bool, error) {
		at, pg := v.Result.Dataset.GeneratedAt, v.Result.Pages
		if dimension == "coverage" {
			return pg.Coverage(r.Context(), at, scope, rangeID, query, page)
		}
		return pg.List(r.Context(), at, scope, rangeID, dimension, query, page)
	})
}

// serveCut serves the body under key in the version's LRU, rendering and
// adding the answer cut gives when absent (404 when it has none, 500 when its
// rows could not be read).
func (s *Server) serveCut(w http.ResponseWriter, r *http.Request, v *Version, key string, cut func() (any, bool, error)) {
	body, ok := v.pages.Get(key)
	if !ok {
		res, found, err := cut()
		if err != nil {
			s.log.Error("list rows", "path", r.URL.Path, "error", err)
			writeError(w, http.StatusInternalServerError, "list rows unavailable")
			return
		}
		if !found {
			writeError(w, http.StatusNotFound, "not found")
			return
		}
		if body, err = Render(http.StatusOK, res, CacheData, LevelFast); err != nil {
			writeError(w, http.StatusInternalServerError, err.Error())
			return
		}
		v.pages.Add(key, body)
	}
	s.write(w, r, v, body)
}
