package server

// The checks every request passes before any work (SEC-PARAMS-1): guard
// refuses text no route can use and a path that is not clean, and every
// route refuses the parameters it does not take (onlyParams, noParams,
// build.Query), so made-up parameters cannot fill a cache with copies of one
// answer or reach the database.

import (
	"maps"
	"net/http"
	"net/url"
	"path"
	"slices"
	"unicode/utf8"

	"github.com/Zeus-Development/atlas/api/internal/build"
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
// that is not clean (a "." or ".." segment, "//": the mux would redirect it),
// 400 for a path, parameter name or value that is not valid text or a query
// that does not parse.
func guard(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Content-Type-Options", "nosniff")
		if p := r.URL.Path; p != "/" && path.Clean(p) != p {
			notFound(w, r)
			return
		}
		if err := checkText(r); err != nil {
			badRequest(w, err)
			return
		}
		next.ServeHTTP(w, r)
	})
}

// checkText is invalid_parameter (format text) for the first path, parameter
// name or value of r that is not valid text, or for a query that does not
// parse (a bad %-escape, a semicolon), which r.URL.Query would drop silently.
func checkText(r *http.Request) error {
	if !validText(r.URL.Path) {
		return build.InvalidFormat("path", r.URL.Path, build.FormatText)
	}
	q, err := url.ParseQuery(r.URL.RawQuery)
	if err != nil {
		return build.InvalidFormat("query", r.URL.RawQuery, build.FormatText)
	}
	for _, k := range slices.Sorted(maps.Keys(q)) {
		if !validText(k) {
			return build.InvalidFormat("query", k, build.FormatText)
		}
		for _, v := range q[k] {
			if !validText(v) {
				return build.InvalidFormat(k, v, build.FormatText)
			}
		}
	}
	return nil
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

// noParams is h for a route that takes no parameter: any query, even one
// without a name ("?&"), answers 400 unknown_parameter.
func noParams(h http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.URL.RawQuery == "" {
			h(w, r)
		} else if onlyParams(w, r.URL.Query()) {
			badRequest(w, build.UnknownParameter(r.URL.RawQuery))
		}
	}
}
