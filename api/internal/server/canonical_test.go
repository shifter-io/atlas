package server

import (
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"

	"github.com/shifter-io/atlas/api/internal/build"
)

// Every request has one canonical form: another spelling of the same path or
// query (an escape where none is needed or in small letters, parameters in
// another order, an empty pair) answers an uncached 308 to the canonical form,
// relative so it resolves under a proxy's path prefix too, and is not
// computed; the canonical form answers itself.
func TestRequestsAreRedirectedToTheirCanonicalForm(t *testing.T) {
	t.Parallel()
	src := &fakeSource{rev: 1, events: events(30), complete: false}
	s := newTestServer(t, src)
	h := s.Handler()
	file := s.Current().Dataset.Path()
	cases := map[string]string{
		strings.Replace(file, "/files/", "/%66iles/", 1):   file,
		strings.Replace(file, "/files/", "/fil%65s/", 1):   file,
		"/v1/coverage/alpha%2Dnet":                         "/v1/coverage/alpha-net",
		"/v1/%76ersion":                                    "/v1/version",
		"/v1/version?&":                                    "/v1/version",
		"/v1/search?q=alpha&":                              "/v1/search?q=alpha",
		"/v1/search?&q=alpha":                              "/v1/search?q=alpha",
		"/v1/search?q=%61lpha":                             "/v1/search?q=alpha",
		"/v1/search?q=a%20b":                               "/v1/search?q=a+b",
		"/v1/search?q=%e2%80%ae":                           "/v1/search?q=%E2%80%AE",
		"/v1/search?q":                                     "/v1/search?q=",
		"/v1/rankings?range=7d&kind=residential":           "/v1/rankings?kind=residential&range=7d",
		"/v1/events?target=generic_http&before=5":          "/v1/events?before=5&target=generic_http",
		"/v1/places/city/US|CA|California|Los%20Angeles":   "/v1/places/city/US%7CCA%7CCalifornia%7CLos%20Angeles",
		"/v1/places/city/US%7ccA%7CSant'Antonio?range=7d":  "/v1/places/city/US%7CcA%7CSant%27Antonio?range=7d",
		"/v1/places/city/a%2Fb":                            "/v1/places/city/a%2Fb",
		"/v1/places/city/a%2fb":                            "/v1/places/city/a%2Fb",
		"/healthz?":                                        "/healthz",
		"/?a=1&":                                           "/?a=1",
		"/v1/networks/alpha-net/chart?range=7d&kind=resi":  "/v1/networks/alpha-net/chart?kind=resi&range=7d",
		"/v1/networks/alpha-net?kind=residential&&&&&&&&&": "/v1/networks/alpha-net?kind=residential",
	}
	for p, want := range cases {
		checkCanonical(t, h, p, want)
	}
	if n := src.dbQueries.Load(); n != 0 {
		t.Errorf("the redirects asked the database %d times", n)
	}
	for p, want := range cases {
		if r := getOnce(h, want); r.Code == http.StatusPermanentRedirect {
			t.Errorf("%s: its canonical form %s redirects again to %s", p, want, r.Header().Get("Location"))
		}
	}
}

// checkCanonical checks that p answers an uncached, relative 308 to want, under
// a proxy's path prefix too, or no redirect when p is want.
func checkCanonical(t *testing.T, h http.Handler, p, want string) {
	t.Helper()
	r := getOnce(h, p)
	if p == want {
		if r.Code == http.StatusPermanentRedirect {
			t.Errorf("%s: redirected to %s, but it is canonical", p, r.Header().Get("Location"))
		}
		return
	}
	if r.Code != http.StatusPermanentRedirect || r.Header().Get("Cache-Control") != CacheNone ||
		r.Header().Get("Access-Control-Allow-Origin") != "*" || r.Body.Len() != 0 {
		t.Errorf("%s: %d %v %s", p, r.Code, r.Header(), r.Body.String())
		return
	}
	loc, err := url.Parse(r.Header().Get("Location"))
	if err != nil || loc.IsAbs() || loc.Host != "" || strings.HasPrefix(loc.Path, "/") {
		t.Errorf("%s: Location %q is not relative", p, r.Header().Get("Location"))
		return
	}
	for _, prefix := range []string{"", "/api/site"} {
		base, _ := url.Parse(prefix + p)
		if got := base.ResolveReference(loc).RequestURI(); got != prefix+want {
			t.Errorf("%s under %q: Location %s resolves to %s, want %s", p, prefix, loc, got, prefix+want)
		}
	}
}

// A parameter given twice has no canonical form: 400 too_many_values,
// uncached, before any work; a path that does not start with "/" is 404.
func TestRepeatedParametersAreRefused(t *testing.T) {
	t.Parallel()
	src := &fakeSource{rev: 1, events: events(30), complete: false}
	h := newTestServer(t, src).Handler()
	for p, name := range map[string]string{
		"/v1/rankings?range=7d&range=7d":        "range",
		"/v1/rankings?kind=a&range=7d&range=1y": "range",
		"/v1/events?before=5&before=6":          "before",
		"/v1/version?cb=1&cb=2":                 "cb",
		"/v1/search?q=&q=":                      "q",
	} {
		r := getOnce(h, p)
		want := `{"code":"too_many_values","params":{"max":1,"name":"` + name + `"}}`
		if r.Code != http.StatusBadRequest || r.Header().Get("Cache-Control") != CacheNone || !strings.Contains(r.Body.String(), want) {
			t.Errorf("%s: %d %s, want 400 %s", p, r.Code, r.Body.String(), want)
		}
	}
	if n := src.dbQueries.Load(); n != 0 {
		t.Errorf("refused requests asked the database %d times", n)
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "*", nil))
	if rec.Code != http.StatusNotFound {
		t.Errorf("GET *: %d %s, want 404", rec.Code, rec.Body.String())
	}
}

// Recognising a canonical request costs one pass over its bytes and no
// allocation, however long its query: only a redirect builds the canonical
// form. Not parallel: testing.AllocsPerRun.
func TestACanonicalRequestIsCheckedWithoutAllocating(t *testing.T) {
	kinds := make([]string, 1000)
	for i := range kinds {
		kinds[i] = "unknown_kind_" + strings.Repeat("x", i%7)
	}
	for _, target := range []string{
		"/v1/events?kind=" + strings.Join(kinds, "%2C") + "&limit=5&q=a+b",
		"/v1/places/city/US%7CCA%7CCalifornia%7CLos%20Angeles?range=7d",
		"/v1/networks/alpha-net/chart?kind=residential&range=7d",
	} {
		r := httptest.NewRequest(http.MethodGet, target, nil)
		q := r.URL.Query()
		if loc, err := canonical(r, q); loc != "" || err != nil {
			t.Fatalf("%.80s: not canonical: %q %v", target, loc, err)
		}
		if n := testing.AllocsPerRun(20, func() { canonical(r, q) }); n != 0 { //nolint:errcheck
			t.Errorf("%.80s: %v allocations per check, want 0", target, n)
		}
	}
}

// The one-pass check accepts exactly what url.PathEscape and
// url.Values.Encode spell.
func TestTheCanonicalCheckAgreesWithTheEncoders(t *testing.T) {
	paths := []string{"/", "/v1/files/x", "/v1/%66iles/x", "/a%2Fb", "/a%2fb", "/a%20b", "/a b", "/a+b", "/a%2Bb", "/a$&+:=@b",
		"/a%24b", "/a,b", "/a%2Cb", "/a;b", "/a%3Bb", "/%7E", "/~", "/%C3%A9", "/%c3%a9", "/a%7Cb", "/a|b", "/a'b", "/a%27b", "/%"}
	for _, p := range paths {
		segs := strings.Split(p, "/")
		ok := true
		for i, seg := range segs {
			s, err := url.PathUnescape(seg)
			ok = ok && err == nil
			segs[i] = url.PathEscape(s)
		}
		if want := ok && strings.Join(segs, "/") == p; canonicalPath(p) != want {
			t.Errorf("canonicalPath(%q) = %v, want %v", p, !want, want)
		}
	}
	queries := []string{"", "a=1", "a=1&b=2", "b=2&a=1", "a=1&", "&a=1", "a", "a=", "=x", "a=x+y", "a=x%20y", "a=%7E", "a=~",
		"a=%2C", "a=,", "a=%2c", "a=%3A", "a=:", "a=%E2%80%AE", "a=%e2%80%ae", "a%2Bb=1&a=2", "a=2&a%2Bb=1", "a+b=1&a-b=2",
		"a-b=2&a+b=1", "a~=1&a%C2%80=2", "a%C2%80=2&a~=1", "kind=a%2Cb&limit=5&q=x", "q=%2A", "q=*", "q=%2D", "a=1&&b=2"}
	for _, q := range queries {
		v, err := url.ParseQuery(q)
		if err != nil {
			t.Fatalf("%q: %v", q, err)
		}
		if want := v.Encode() == q; canonicalQuery(q) != want {
			t.Errorf("canonicalQuery(%q) = %v, want %v", q, !want, want)
		}
		if got := encodeQuery(v); got != v.Encode() {
			t.Errorf("encodeQuery(%q) = %q, want %q", q, got, v.Encode())
		}
	}
}

// The guard hands the query it parsed to the route, which reads it without
// parsing the query again.
func TestTheGuardHandsItsParsedQueryOn(t *testing.T) {
	t.Parallel()
	var got string
	h := guard(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		r.URL.RawQuery = ""
		got = build.RequestQuery(r).Get("kind")
	}))
	h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodGet, "/v1/events?kind=a%2Cb&limit=5", nil))
	if got != "a,b" {
		t.Fatalf("the route read kind %q, want the guard's a,b", got)
	}
}
