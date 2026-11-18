package server

import (
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
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
