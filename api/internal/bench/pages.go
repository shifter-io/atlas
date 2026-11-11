// Package bench times every request every Proxy Index page makes against a
// running siteapi, the way the website calls it (`siteapi bench`).
package bench

import (
	"net/url"
	"regexp"
	"strings"

	"github.com/Zeus-Development/proxy-index/api/internal/build"
)

// Ranges are the API's ranges, in the order the pages show them.
var Ranges = []string{"24h", "7d", "30d"}

// PageSpec is one Proxy Index page's request list (docs/api-web-map.md,
// section 10, "After section 11"): the existing routes it calls and the
// registered answers it draws. Routes are templates over {range}, {slug},
// {kind} and {q}; a route with {range} is requested once per range.
type PageSpec struct {
	Name string
	// PerNetwork makes one page per network and kind, with {slug} and {kind}.
	PerNetwork bool
	Ranges     []string
	Routes     []string
	// Answers are the paths of the registered samples the page draws;
	// {slug} matches one path segment, and a sample under a path matches it.
	Answers []string
}

// Specs is every page's request list. PAGE-LATENCY-1 and the bench both
// expand it, so the two never disagree.
var Specs = []PageSpec{
	{Name: "/proxy-index", Ranges: Ranges, Routes: []string{"/v1/rankings?range={range}", "/v1/events"}, Answers: []string{"/v1/home"}},
	{Name: "/proxy-index/targets", Ranges: Ranges, Answers: []string{"/v1/targets", "/v1/meta"}},
	{Name: "/proxy-index/networks", Ranges: Ranges, Routes: []string{"/v1/rankings?range={range}"}, Answers: []string{"/v1/meta"}},
	{Name: "/proxy-index/networks/{slug}", PerNetwork: true, Ranges: Ranges, Routes: []string{
		"/v1/networks/{slug}?kind={kind}",
		"/v1/networks/{slug}/chart?kind={kind}&range={range}",
		"/v1/networks/{slug}/coverage?kind={kind}&range={range}",
		"/v1/networks/{slug}/coverage?kind={kind}&range={range}&q={q}",
		"/v1/rankings?range={range}",
	}, Answers: []string{"/v1/targets", "/v1/overlap", "/v1/networks/{slug}"}},
	{Name: "/proxy-index/compare", Ranges: Ranges, Routes: []string{"/v1/rankings?range={range}"}, Answers: []string{"/v1/targets", "/v1/meta"}},
	{Name: "/proxy-index/overlap", Ranges: []string{"30d"}, Routes: []string{"/v1/rankings?range={range}"}, Answers: []string{"/v1/overlap"}},
	{Name: "/proxy-index/true-cost", Ranges: Ranges, Routes: []string{"/v1/rankings?range={range}"}, Answers: []string{"/v1/targets"}},
	{Name: "/proxy-index/changelog", Ranges: Ranges, Routes: []string{"/v1/networks"}, Answers: []string{"/v1/changelog"}},
	{Name: "/proxy-index/live-tape", Ranges: Ranges, Routes: []string{"/v1/events?limit=200", "/v1/events"}, Answers: []string{"/v1/events/stats"}},
	{Name: "search palette", Routes: []string{"/v1/search-index", "/v1/search?q={q}"}},
	{Name: "freshness", Routes: []string{"/v1/version"}},
}

// OtherAnswers is the page of the registered samples no page draws, so
// every sample is still timed.
const OtherAnswers = "other registered answers"

// Network is one network of /v1/networks.
type Network struct {
	Slug  string
	Kinds []string
}

// Shape is what the expansion needs from the running server.
type Shape struct {
	Networks []Network
	// Query is the q= of a list search (a country code); Search is the
	// q= of /v1/search (a network slug); Country is that country code as
	// /v1/countries gives it.
	Query, Search, Country string
}

// Page is one page view and its request URLs (path and query), in order.
type Page struct {
	Name     string   `json:"name"`
	Requests []string `json:"requests"`
}

// Expand is every page of Specs for sh, with the samples of answers.
func Expand(specs []PageSpec, sh Shape, answers []build.PageAnswer) []Page {
	used := map[string]bool{}
	var out []Page
	for _, spec := range specs {
		if !spec.PerNetwork {
			out = append(out, expandPage(spec, spec.Name, map[string]string{"q": sh.Search}, answers, used))
			continue
		}
		for _, n := range sh.Networks {
			for _, kind := range n.Kinds {
				vars := map[string]string{"slug": n.Slug, "kind": kind, "q": sh.Query}
				name := strings.ReplaceAll(spec.Name, "{slug}", n.Slug) + " (" + kind + ")"
				out = append(out, expandPage(spec, name, vars, answers, used))
			}
		}
	}
	other := Page{Name: OtherAnswers}
	for _, a := range answers {
		for _, s := range a.Samples {
			if !used[s] {
				other.Requests = appendNew(other.Requests, expandSample(a, s, Ranges, nil)...)
			}
		}
	}
	if len(other.Requests) > 0 {
		out = append(out, other)
	}
	return out
}

func expandPage(spec PageSpec, name string, vars map[string]string, answers []build.PageAnswer, used map[string]bool) Page {
	p := Page{Name: name}
	for _, route := range spec.Routes {
		p.Requests = appendNew(p.Requests, expandRoute(route, spec.Ranges, vars)...)
	}
	for _, a := range answers {
		for _, s := range a.Samples {
			if drawn(spec.Answers, s) {
				used[s] = true
				p.Requests = appendNew(p.Requests, expandSample(a, s, spec.Ranges, vars)...)
			}
		}
	}
	return p
}

// expandRoute fills a route template, once per range when it has {range}.
func expandRoute(route string, ranges []string, vars map[string]string) []string {
	fill := func(r string) string {
		out := strings.ReplaceAll(route, "{range}", r)
		for k, v := range vars {
			esc := url.QueryEscape(v)
			if k == "slug" {
				esc = url.PathEscape(v)
			}
			out = strings.ReplaceAll(out, "{"+k+"}", esc)
		}
		return out
	}
	if !strings.Contains(route, "{range}") {
		return []string{fill("")}
	}
	out := make([]string, 0, len(ranges))
	for _, r := range ranges {
		out = append(out, fill(r))
	}
	return out
}

// expandSample is a registered sample on one page: its {slug} segment (by
// its route) set to the page's network, its kind= to the page's kind and,
// when it has a range=, one URL per range of the page.
func expandSample(a build.PageAnswer, sample string, ranges []string, vars map[string]string) []string {
	u, err := url.Parse(sample)
	if err != nil {
		return []string{sample}
	}
	if slug := vars["slug"]; slug != "" {
		segs := strings.Split(u.Path, "/")
		for i, seg := range strings.Split(strings.TrimPrefix(a.Route, "GET "), "/") {
			if seg == "{slug}" && i < len(segs) {
				segs[i] = slug
			}
		}
		u.Path = strings.Join(segs, "/")
	}
	q := u.Query()
	if kind := vars["kind"]; kind != "" && q.Has("kind") {
		q.Set("kind", kind)
	}
	if !q.Has("range") {
		u.RawQuery = q.Encode()
		return []string{u.String()}
	}
	out := make([]string, 0, len(ranges))
	for _, r := range ranges {
		q.Set("range", r)
		u.RawQuery = q.Encode()
		out = append(out, u.String())
	}
	return out
}

// drawn reports whether a sample's path is one of paths or under one.
func drawn(paths []string, sample string) bool {
	path, _, _ := strings.Cut(sample, "?")
	for _, p := range paths {
		pat := strings.ReplaceAll(regexp.QuoteMeta(p), regexp.QuoteMeta("{slug}"), "[^/]+")
		if regexp.MustCompile("^" + pat + "(/.*)?$").MatchString(path) {
			return true
		}
	}
	return false
}

func appendNew(list []string, add ...string) []string {
	for _, a := range add {
		dup := false
		for _, l := range list {
			dup = dup || l == a
		}
		if !dup {
			list = append(list, a)
		}
	}
	return list
}

// Class is the budget class of a request (PAGE-LATENCY-1's table).
func Class(request string) string {
	u, err := url.Parse(request)
	if err != nil {
		return ClassAnswer
	}
	p := u.Path
	switch {
	case p == "/v1/events":
		return ClassEvents
	case p == "/v1/search":
		return ClassSearch
	case listPath.MatchString(p) && u.Query().Get("q") != "":
		return ClassListSearch
	case listPath.MatchString(p):
		return ClassList
	}
	return ClassAnswer
}

// listPath is the long-list pages the server renders on request.
var listPath = regexp.MustCompile(`^/v1/(networks/[^/]+/(places|coverage)|countries/[^/]+/(cities|regions)|places/[^/]+/[^/]+)$`)

// The budget classes.
const (
	ClassAnswer     = "answer"      // a precomputed answer
	ClassEvents     = "events"      // /v1/events
	ClassSearch     = "search"      // /v1/search?q=
	ClassList       = "list"        // a list page: cold is its first render
	ClassListSearch = "list-search" // a q= search over a long list
)
