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
		next.ServeHTTP(w, build.WithQuery(r, q))
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
// one copy of it. A canonical request is recognised in one pass over its
// bytes, without allocating; only a redirect spells the canonical form out.
func canonical(r *http.Request, q url.Values) (string, error) {
	if k, ok := repeated(q); ok {
		return "", build.TooManyValues(k, 1)
	}
	raw := sentPath(r)
	if canonicalPath(raw) && canonicalQuery(r.URL.RawQuery) && !r.URL.ForceQuery {
		return "", nil
	}
	segs := strings.Split(raw, "/")
	for i, seg := range segs {
		s, err := url.PathUnescape(seg)
		if err != nil {
			return "", build.InvalidFormat("path", raw, build.FormatText)
		}
		segs[i] = url.PathEscape(s)
	}
	p, query := strings.Join(segs, "/"), encodeQuery(q)
	loc := "./" + strings.Repeat("../", strings.Count(p, "/")-1) + strings.TrimPrefix(p, "/")
	if query != "" {
		loc += "?" + query
	}
	return loc, nil
}

// sentPath is r's path as the client sent it: from the request line when
// the server has it (no copy), else the URL's escaped path.
func sentPath(r *http.Request) string {
	if p, _, _ := strings.Cut(r.RequestURI, "?"); strings.HasPrefix(p, "/") {
		return p
	}
	if r.URL.RawPath != "" {
		return r.URL.RawPath
	}
	return r.URL.EscapedPath()
}

// encodeQuery is q as url.Values.Encode spells it, written in runs of the
// bytes kept as they are, so a long value costs a few copies rather than one
// step per byte.
func encodeQuery(q url.Values) string {
	var b []byte
	for _, k := range slices.Sorted(maps.Keys(q)) {
		for _, v := range q[k] {
			if len(b) > 0 {
				b = append(b, '&')
			}
			b = append(appendQueryEscaped(b, k), '=')
			b = appendQueryEscaped(b, v)
		}
	}
	return string(b)
}

// appendQueryEscaped is b with s appended as url.QueryEscape spells it.
func appendQueryEscaped(b []byte, s string) []byte {
	const hex = "0123456789ABCDEF"
	start := 0
	for i := 0; i < len(s); i++ {
		c := s[i]
		if unreserved(c) {
			continue
		}
		b = append(b, s[start:i]...)
		if c == ' ' {
			b = append(b, '+')
		} else {
			b = append(b, '%', hex[c>>4], hex[c&15])
		}
		start = i + 1
	}
	return append(b, s[start:]...)
}

// repeated is the first name, in byte order, that q holds more than once.
func repeated(q url.Values) (string, bool) {
	name, found := "", false
	for k, v := range q {
		if len(v) > 1 && (!found || k < name) {
			name, found = k, true
		}
	}
	return name, found
}

// canonicalPath reports whether each "/"-separated segment of the escaped
// path p is as url.PathEscape spells it: its bytes are kept as they are or
// escaped in capitals exactly when url.PathEscape escapes them.
func canonicalPath(p string) bool {
	for i := 0; i < len(p); i++ {
		switch c := p[i]; {
		case c == '/' || pathKept(c):
		case c == '%' && i+2 < len(p) && upperHex(p[i+1]) && upperHex(p[i+2]) && !pathKept(unhex(p[i+1])<<4|unhex(p[i+2])):
			i += 2
		default:
			return false
		}
	}
	return true
}

// canonicalQuery reports whether the raw query q is as url.Values.Encode
// spells its parameters: name=value pairs joined by "&", each name and value
// as url.QueryEscape spells it, the names in increasing order (each once).
func canonicalQuery(q string) bool {
	prev := ""
	for i := 0; q != ""; i++ {
		pair, rest, more := strings.Cut(q, "&")
		k, v, ok := strings.Cut(pair, "=")
		if !ok || (more && rest == "") || !queryEscaped(k) || !queryEscaped(v) {
			return false
		}
		name := k
		if strings.ContainsAny(k, "%+") {
			name, _ = url.QueryUnescape(k)
		}
		if i > 0 && name <= prev {
			return false
		}
		prev, q = name, rest
	}
	return true
}

// queryEscaped reports whether s is as url.QueryEscape spells its bytes:
// letters, digits and "-_.~" as they are, a space as "+", every other byte
// escaped in capitals.
func queryEscaped(s string) bool {
	for i := 0; i < len(s); i++ {
		switch c := s[i]; {
		case c == '+' || unreserved(c):
		case c == '%' && i+2 < len(s) && upperHex(s[i+1]) && upperHex(s[i+2]):
			if b := unhex(s[i+1])<<4 | unhex(s[i+2]); b == ' ' || unreserved(b) {
				return false
			}
			i += 2
		default:
			return false
		}
	}
	return true
}

// unreserved reports whether c is a letter, a digit or one of "-_.~", which
// no escape function escapes.
func unreserved(c byte) bool {
	return 'a' <= c && c <= 'z' || 'A' <= c && c <= 'Z' || '0' <= c && c <= '9' || c == '-' || c == '_' || c == '.' || c == '~'
}

// pathKept reports whether url.PathEscape keeps c as it is.
func pathKept(c byte) bool {
	return unreserved(c) || strings.IndexByte("$&+:=@", c) >= 0
}

func upperHex(c byte) bool { return '0' <= c && c <= '9' || 'A' <= c && c <= 'F' }

func unhex(c byte) byte {
	if c <= '9' {
		return c - '0'
	}
	return c - 'A' + 10
}

// redirectCanonical answers 308 to loc, the request's canonical form,
// uncached: a cache whose key does not tell parameter orders or escapes
// apart holds the canonical URL under the same key, so a stored redirect
// would be answered for the canonical URL too.
func redirectCanonical(w http.ResponseWriter, loc string) {
	h := w.Header()
	h.Set("Location", loc)
	h.Set("Cache-Control", CacheNone)
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
		} else if onlyParams(w, build.RequestQuery(r)) {
			badRequest(w, build.UnknownParameter(r.URL.RawQuery))
		}
	}
}
