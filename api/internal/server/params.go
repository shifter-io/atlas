package server

// The checks every request passes before any work (SEC-PARAMS-1): guard
// refuses text no route can use and a path that is not clean, redirects a
// request to its one canonical form (canonical), and every route refuses the
// parameters it does not take (onlyParams, noParams, build.Query), so neither
// made-up parameters nor other spellings of one URL can fill a cache with
// copies of one answer or reach the database.

import (
	"maps"
	"net/http"
	"net/url"
	"path"
	"slices"
	"strings"
	"unicode/utf8"

	"github.com/shifter-io/atlas/api/internal/build"
)

// routeMux is the API's mux with the patterns it routes, so a test can ask
// every route.
type routeMux struct {
	*http.ServeMux
	patterns []string
}

// HandleFunc routes pattern to h and records it.
func (m *routeMux) HandleFunc(pattern string, h func(http.ResponseWriter, *http.Request)) {
	m.ServeMux.HandleFunc(pattern, h)
	m.patterns = append(m.patterns, pattern)
}

// guard sets nosniff on every answer and answers before next: 404 for a path
// that is not clean (a "." or ".." segment, "//": the mux would redirect it)
// or does not start with "/", 400 for a path, parameter name or value that is
// not valid text, a query that does not parse or a parameter given twice, and
// 308 to the canonical form of a request in any other form (canonical).
func guard(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Content-Type-Options", "nosniff")
		if p := r.URL.Path; p != "/" && (path.Clean(p) != p || !strings.HasPrefix(p, "/")) {
			notFound(w, r)
			return
		}
		q, err := checkText(r)
		if err != nil {
			badRequest(w, err)
			return
		}
		loc, err := canonical(r, q)
		if err != nil {
			badRequest(w, err)
			return
		}
		if loc != "" {
			redirectCanonical(w, loc)
			return
		}
		next.ServeHTTP(w, r)
	})
}

// checkText is r's query, or invalid_parameter (format text) for the first
// path, parameter name or value of r that is not valid text, or for a query
// that does not parse (a bad %-escape, a semicolon), which r.URL.Query would
// drop silently.
func checkText(r *http.Request) (url.Values, error) {
	if !validText(r.URL.Path) {
		return nil, build.InvalidFormat("path", r.URL.Path, build.FormatText)
	}
	q, err := url.ParseQuery(r.URL.RawQuery)
	if err != nil {
		return nil, build.InvalidFormat("query", r.URL.RawQuery, build.FormatText)
	}
	for _, k := range slices.Sorted(maps.Keys(q)) {
		if !validText(k) {
			return nil, build.InvalidFormat("query", k, build.FormatText)
		}
		for _, v := range q[k] {
			if !validText(v) {
				return nil, build.InvalidFormat(k, v, build.FormatText)
			}
		}
	}
	return q, nil
}

// canonical is where a request that is not in its one canonical form is
// redirected (relative, so it resolves under a proxy's path prefix), empty
// for a canonical request, and too_many_values (max 1) for a parameter given
// twice, which has none. The canonical form escapes each path segment as
// url.PathEscape does and holds the query's parameters sorted by name,
// encoded as url.Values.Encode does: no empty pair or query, every escape in
// capitals. So each answer has one URL, and a cache keyed on the URL keeps
// one copy of it.
func canonical(r *http.Request, q url.Values) (string, error) {
	for _, k := range slices.Sorted(maps.Keys(q)) {
		if len(q[k]) > 1 {
			return "", build.TooManyValues(k, 1)
		}
	}
	raw := r.URL.RawPath
	if raw == "" {
		raw = r.URL.EscapedPath()
	}
	segs := strings.Split(raw, "/")
	for i, seg := range segs {
		s, err := url.PathUnescape(seg)
		if err != nil {
			return "", build.InvalidFormat("path", raw, build.FormatText)
		}
		segs[i] = url.PathEscape(s)
	}
	p, query := strings.Join(segs, "/"), q.Encode()
	if p == raw && query == r.URL.RawQuery && !r.URL.ForceQuery {
		return "", nil
	}
	loc := "./" + strings.Repeat("../", strings.Count(p, "/")-1) + strings.TrimPrefix(p, "/")
	if query != "" {
		loc += "?" + query
	}
	return loc, nil
}

// redirectCanonical answers 308 to loc, the request's canonical form, cached
// for CacheCanonical: the redirect never changes, so a CDN keeps it and
// answers the same request again without the API.
func redirectCanonical(w http.ResponseWriter, loc string) {
	h := w.Header()
	h.Set("Location", loc)
	h.Set("Cache-Control", CacheCanonical)
	h.Set("Access-Control-Allow-Origin", "*")
	w.WriteHeader(http.StatusPermanentRedirect)
}

// validText reports whether s is UTF-8 without a control character (U+0000
// to U+001F, U+007F).
func validText(s string) bool {
	if !utf8.ValidString(s) {
		return false
	}
	for i := 0; i < len(s); i++ {
		if s[i] < 0x20 || s[i] == 0x7f {
			return false
		}
	}
	return true
}

// onlyParams answers 400 and returns false when the query has a parameter
// outside allowed.
func onlyParams(w http.ResponseWriter, q url.Values, allowed ...string) bool {
	for _, k := range slices.Sorted(maps.Keys(q)) {
		if !slices.Contains(allowed, k) {
			badRequest(w, build.UnknownParameter(k))
			return false
		}
	}
	return true
}

// noParams is h for a route that takes no parameter: any query answers 400
// unknown_parameter (one without a name, "?&", is redirected first: guard).
func noParams(h http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.URL.RawQuery == "" {
			h(w, r)
		} else if onlyParams(w, r.URL.Query()) {
			badRequest(w, build.UnknownParameter(r.URL.RawQuery))
		}
	}
}
