package server

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/shifter-io/atlas/api/internal/bench"
	"github.com/shifter-io/atlas/api/internal/build"
	"github.com/shifter-io/atlas/api/internal/testscale"
)

// websiteNetworks is how many networks the whole-website check's build has.
const websiteNetworks = 18

// Every request the website sends, for each of the build's 18 networks and
// each range: home, rankings, targets and overlap per range, meta, networks,
// events?limit=500, changelog, each network's answer, pricing history,
// chart?range= and coverage?range=&page=1, and every request of every page
// the website draws (bench.Specs). Each is asked as the website writes it
// and in every other order of its parameters, directly and through a cache
// keyed without the parameter order, following redirects as a browser does:
// each answers 200 with valid JSON after at most one redirect, and no
// redirect points at the URL it answered.
func TestTheWholeWebsiteAnswersInAtMostOneRedirect(t *testing.T) {
	t.Parallel()
	h, res := websiteBuild(t)
	requests := websiteFetchList(t, res)
	origin, cdn := &orderBlindCache{h: h}, &orderBlindCache{h: h, store: map[string]*httptest.ResponseRecorder{}}
	asked := 0
	for _, req := range requests {
		path, raw, _ := strings.Cut(req, "?")
		for _, order := range permutations(strings.Split(raw, "&")) {
			target := path
			if raw != "" {
				target += "?" + strings.Join(order, "&")
			}
			for _, c := range []*orderBlindCache{cdn, origin} {
				asked++
				rec, at, hops, self := follow(c, target)
				if rec.Code != http.StatusOK || !json.Valid(rec.Body.Bytes()) || hops > 1 || self {
					t.Errorf("%s (cached %v): %d after %d redirects at %s (self %v)", target, c.store != nil, rec.Code, hops, at, self)
				}
			}
		}
	}
	t.Logf("%d requests, %d asked", len(requests), asked)
}

// websiteBuild is a v4 build of websiteNetworks networks, served.
func websiteBuild(t *testing.T) (http.Handler, *build.Result) {
	t.Helper()
	in, err := testscale.NetworkInputs(testscale.NetworkShape{Shape: testscale.Shape{Networks: websiteNetworks, Countries: 1, Exits: 3,
		Tail: 30, CityAsn: 60}, Requested: 2})
	if err != nil {
		t.Fatal(err)
	}
	res, err := build.Build(in)
	if err != nil {
		t.Fatal(err)
	}
	v, err := RenderVersion(1, res, Options{FilesDir: t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	srv := New(&fakeSource{}, Options{})
	srv.current.Store(v)
	return srv.Handler(), res
}

// websiteFetchList is the website's requests for res: the fixed list per
// network and range, then every page's requests (bench.Specs).
func websiteFetchList(t *testing.T, res *build.Result) []string {
	t.Helper()
	sh := bench.Shape{Query: "aa", Search: "net00"}
	out := []string{"/v1/meta", "/v1/networks", "/v1/events?limit=500", "/v1/changelog"}
	for _, r := range build.RangeIDs {
		for _, p := range []string{"/v1/home", "/v1/rankings", "/v1/targets", "/v1/overlap", "/v1/changelog"} {
			out = append(out, p+"?range="+r)
		}
	}
	for _, n := range res.Pages.Networks.Networks {
		sh.Networks = append(sh.Networks, bench.Network{Slug: n.Slug, Kinds: n.Kinds})
		base := "/v1/networks/" + n.Slug
		out = append(out, base, base+"/pricing-history")
		for _, r := range build.RangeIDs {
			out = append(out, base+"/chart?range="+r, base+"/coverage?range="+r+"&page=1")
		}
	}
	if len(sh.Networks) != websiteNetworks {
		t.Fatalf("%d networks, want %d", len(sh.Networks), websiteNetworks)
	}
	for _, page := range bench.Expand(bench.Specs, sh, build.Answers()) {
		out = append(out, page.Requests...)
	}
	return out
}
