package server

import (
	"net/http"
	"net/http/httptest"
	"net/url"
	"slices"
	"strings"
	"testing"
)

// websiteRequests are the routes the website reads, each with every
// parameter it sends (a value the small v4 build answers 200).
var websiteRequests = []struct {
	path   string
	params [][2]string
}{
	{"/v1/home", [][2]string{{"range", "7d"}}},
	{"/v1/rankings", [][2]string{{"kind", "residential"}, {"range", "7d"}}},
	{"/v1/targets", [][2]string{{"engine", "chrome"}, {"kind", "residential"}, {"range", "7d"}}},
	{"/v1/overlap", [][2]string{{"kind", "residential"}, {"range", "7d"}}},
	{"/v1/meta", nil},
	{"/v1/networks", nil},
	{"/v1/events", [][2]string{{"kind", "pulse"}, {"limit", "50"}, {"q", "net"}}},
	{"/v1/changelog", [][2]string{{"range", "7d"}}},
	{"/v1/networks/net00", [][2]string{{"kind", "residential"}}},
	{"/v1/networks/net00/coverage", [][2]string{{"kind", "residential"}, {"page", "1"}, {"q", "a"}, {"range", "24h"}}},
	{"/v1/networks/net00/pricing-history", [][2]string{{"kind", "residential"}}},
	{"/v1/networks/net00/chart", [][2]string{{"kind", "residential"}, {"range", "7d"}}},
}

// For every route the website reads, every subset of its parameters in
// every order, asked through a cache whose key ignores the parameters'
// order (as a CDN may sort them), non-canonical spellings first: the
// canonical URL answers 200 with no redirect, and any other spelling answers
// 200 after at most one redirect, to the canonical URL (none when the cache
// answers it with the canonical URL's copy). The canonical form is a fixed
// point, and no stored redirect is ever answered for it.
func TestEveryWebsiteRequestReachesItsAnswerInAtMostOneRedirect(t *testing.T) {
	t.Parallel()
	h, _ := smallV4(t)
	origin := &orderBlindCache{h: h} // no store: every request reaches the API
	for _, req := range websiteRequests {
		for _, set := range subsets(req.params) {
			cdn := &orderBlindCache{h: h, store: map[string]*httptest.ResponseRecorder{}}
			canonical := req.path + query(set, false)
			orders := permutations(set)
			slices.SortStableFunc(orders, func(a, b [][2]string) int { // non-canonical spellings first
				return strings.Compare(query(b, true), query(a, true))
			})
			for round := range 2 { // a cold cache, then a warm one
				for _, order := range orders {
					for _, raw := range []string{req.path + query(order, true) + "&", req.path + query(order, true)} {
						if len(order) == 0 && strings.HasSuffix(raw, "&") {
							raw = req.path + "?"
						}
						checkVisit(t, cdn, raw, canonical, round)
						want := 0
						if raw != canonical {
							want = 1
						}
						if hops := checkVisit(t, origin, raw, canonical, round); hops != want {
							t.Errorf("%s: %d redirects from the API itself, want %d", raw, hops, want)
						}
					}
				}
			}
		}
	}
}

// checkVisit follows raw through cdn as a browser does (at most 5
// redirects) and checks it answers 200: canonical without a redirect, any
// other spelling after at most one, to canonical. It returns the redirects.
func checkVisit(t *testing.T, cdn *orderBlindCache, raw, canonical string, round int) int {
	t.Helper()
	rec, target, hops, _ := follow(cdn, raw)
	if rec.Code != http.StatusOK || hops > 1 || (raw == canonical && hops != 0) || (hops == 1 && target != canonical) {
		t.Errorf("round %d %s: %d after %d redirects at %s, want 200 at %s after at most one", round, raw, rec.Code, hops, target, canonical)
	}
	return hops
}

// follow requests raw through cdn and follows its redirects (302 and 308)
// as a browser does, at most 5: the last answer, its URL, the redirects and
// whether one pointed at the URL that answered it.
func follow(cdn *orderBlindCache, raw string) (rec *httptest.ResponseRecorder, target string, hops int, self bool) {
	target = raw
	rec = cdn.get(target)
	for (rec.Code == http.StatusPermanentRedirect || rec.Code == http.StatusFound) && hops < 5 {
		base, _ := url.Parse(target)
		loc, err := url.Parse(rec.Header().Get("Location"))
		if err != nil {
			return rec, target, hops, false
		}
		next := base.ResolveReference(loc).RequestURI()
		self = self || next == target
		target, hops = next, hops+1
		rec = cdn.get(target)
	}
	return rec, target, hops, self
}

// orderBlindCache is a shared cache in front of h whose key is the path and
// the query's pairs sorted, as raw text: it stores what a CDN may store (with
// a nil store, nothing).
type orderBlindCache struct {
	h     http.Handler
	store map[string]*httptest.ResponseRecorder
}

func (c *orderBlindCache) get(target string) *httptest.ResponseRecorder {
	path, raw, _ := strings.Cut(target, "?")
	pairs := strings.Split(raw, "&")
	slices.Sort(pairs)
	key := path + "?" + strings.Trim(strings.Join(pairs, "&"), "&")
	if rec, ok := c.store[key]; ok {
		return rec
	}
	rec := getOnce(c.h, target)
	if cc := rec.Header().Get("Cache-Control"); c.store != nil && strings.Contains(cc, "public") && !strings.Contains(cc, "no-store") {
		c.store[key] = rec
	}
	return rec
}

// query is "?" and params joined in their order ("" for none); sorted
// unless asGiven.
func query(params [][2]string, asGiven bool) string {
	if len(params) == 0 {
		return ""
	}
	if !asGiven {
		params = slices.Clone(params)
		slices.SortFunc(params, func(a, b [2]string) int { return strings.Compare(a[0], b[0]) })
	}
	parts := make([]string, len(params))
	for i, p := range params {
		parts[i] = url.QueryEscape(p[0]) + "=" + url.QueryEscape(p[1])
	}
	return "?" + strings.Join(parts, "&")
}

// subsets is every subset of params, the empty one included.
func subsets(params [][2]string) [][][2]string {
	out := [][][2]string{nil}
	for _, p := range params {
		for _, s := range out {
			out = append(out, append(slices.Clone(s), p))
		}
	}
	return out
}

// permutations is every order of xs.
func permutations[T any](xs []T) [][]T {
	if len(xs) <= 1 {
		return [][]T{xs}
	}
	var out [][]T
	for i, x := range xs {
		rest := append(slices.Clone(xs[:i]), xs[i+1:]...)
		for _, tail := range permutations(rest) {
			out = append(out, append([]T{x}, tail...))
		}
	}
	return out
}
