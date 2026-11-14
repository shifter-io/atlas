package server

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"golang.org/x/sync/errgroup"

	"github.com/Zeus-Development/atlas/api/internal/build"
)

// The English guard on a served API (ENGLISH-GUARD-1, #976).

// TestNoEnglishInAnswersLive runs the guard on a served API
// (SITEAPI_SCAN_URL, as TestNoRemovedProseFieldLive): every range of the
// home, rankings, targets and overlap, the meta, networks, events, changelog,
// search and N/A report answers, every network's answer, chart, places,
// coverage, pricing history, reviews and runs, every country with its cities
// and regions, the dataset file, the error answers and /healthz. It fails on
// English the pending list does not name; listed paths it finds clean are
// logged, as the deployed build may lag main.
func TestNoEnglishInAnswersLive(t *testing.T) {
	base := strings.TrimRight(os.Getenv("SITEAPI_SCAN_URL"), "/")
	if base == "" {
		t.Skip("SITEAPI_SCAN_URL not set")
	}
	hits := liveEnglish(t, base)
	dumpEnglish(t, hits)
	checkPending(t, hits, readPending(t), false)
}

// TestEnglishLiveMode runs the live mode on the scan build, served: every
// English path it finds is listed, so it groups bodies as the build's scan
// does, a network's answer included.
func TestEnglishLiveMode(t *testing.T) {
	t.Parallel()
	_, srv := scanServer(t)
	ts := httptest.NewServer(srv.Handler())
	defer ts.Close()
	bodies := liveBodies(t, ts.URL)
	networks := 0
	for p := range bodies {
		if slug, ok := strings.CutPrefix(p, "/v1/networks/"); ok && slug != "all" && !strings.Contains(slug, "/") {
			networks++
		}
	}
	if networks == 0 {
		t.Errorf("no network answer scanned: %d bodies", len(bodies))
	}
	checkPending(t, scanEnglish(bodies), readPending(t), false)
}

// liveEnglish is every English path of the API served at base.
func liveEnglish(t *testing.T, base string) map[string]englishHit {
	t.Helper()
	return scanEnglish(liveBodies(t, base))
}

// liveBodies is every body of the API served at base that the live scan reads.
func liveBodies(t *testing.T, base string) map[string][]byte {
	t.Helper()
	client := &http.Client{Timeout: 60 * time.Second}
	bodies := map[string][]byte{}
	fetchAll(t, client, base, bodies, liveIndexPaths())
	fetchAll(t, client, base, bodies, liveNetworkPaths(t, bodies["/v1/networks"]))
	fetchAll(t, client, base, bodies, liveCountryPaths(t, bodies["/v1/countries"]))
	t.Logf("scanned %d bodies", len(bodies)) // never the base URL
	return bodies
}

// liveIndexPaths are the answers without a network or a country, the error
// answers and /healthz.
func liveIndexPaths() []string {
	paths := []string{"/v1/meta", "/v1/networks", "/v1/countries", "/v1/targets", "/v1/events", "/v1/events/stats", "/v1/changelog",
		"/v1/search?q=a", "/v1/search?q=amazon", "/v1/coverage", "/v1/dataset", "/v1/networks/all",
		"/v1/networks/all/chart", "/v1/networks/all/pricing-history", "/v1/networks/all/reviews", "/v1/networks/all/runs", "/healthz"}
	paths = append(paths, errorPaths...)
	for _, r := range build.Ranges {
		for _, p := range []string{"/v1/home", "/v1/rankings", "/v1/targets", "/v1/overlap"} {
			paths = append(paths, p+"?range="+r.ID)
		}
	}
	return paths
}

// liveNetworkPaths are every network's answers, from /v1/networks.
func liveNetworkPaths(t *testing.T, list []byte) []string {
	var doc struct {
		Networks []struct {
			Slug string `json:"slug"`
		} `json:"networks"`
	}
	if err := json.Unmarshal(list, &doc); err != nil || len(doc.Networks) == 0 {
		t.Fatalf("/v1/networks: %d networks, %v", len(doc.Networks), err)
	}
	var paths []string
	for _, n := range doc.Networks {
		p := "/v1/networks/" + n.Slug
		paths = append(paths, p, p+"/chart", p+"/coverage", p+"/pricing-history", p+"/reviews", p+"/runs", "/v1/coverage/"+n.Slug)
		for _, d := range []string{"country", "region", "city", "asn"} {
			paths = append(paths, p+"/places?dimension="+d)
		}
	}
	return paths
}

// liveCountryPaths are every country's answer, cities and regions, from
// /v1/countries.
func liveCountryPaths(t *testing.T, list []byte) []string {
	var doc struct {
		Countries []struct {
			Code string `json:"countryCode"`
		} `json:"countries"`
	}
	if err := json.Unmarshal(list, &doc); err != nil {
		t.Fatalf("/v1/countries: %v", err)
	}
	var paths []string
	for _, c := range doc.Countries {
		p := "/v1/countries/" + c.Code
		paths = append(paths, p, p+"/cities", p+"/regions")
	}
	return paths
}

// fetchAll adds the body of every path to bodies, eight at a time: of the
// errorPaths every body, else only a 200's (an unmeasured network has no
// chart or places, as the scan build's pages).
func fetchAll(t *testing.T, client *http.Client, base string, bodies map[string][]byte, paths []string) {
	t.Helper()
	var mu sync.Mutex
	var g errgroup.Group
	g.SetLimit(8)
	for _, p := range paths {
		g.Go(func() error {
			resp, err := client.Get(base + p)
			var ue *url.Error
			if errors.As(err, &ue) {
				err = ue.Err // its text holds the base URL, which is never printed
			}
			if err != nil {
				return fmt.Errorf("%s: %w", p, err)
			}
			defer resp.Body.Close()
			b, err := io.ReadAll(resp.Body)
			if err != nil {
				return fmt.Errorf("%s: %w", p, err)
			}
			mu.Lock()
			if resp.StatusCode == http.StatusOK || slices.Contains(errorPaths, p) {
				bodies[p] = b
			}
			mu.Unlock()
			return nil
		})
	}
	if err := g.Wait(); err != nil {
		t.Fatal(err)
	}
}

func TestEnglishGroup(t *testing.T) {
	t.Parallel()
	for name, want := range map[string]string{
		"/v1/networks/net00/residential": "/v1/networks/*", "/v1/networks/all/all": "/v1/networks/all", "/v1/home/24h": "/v1/home",
		"/v1/charts/net00/residential/24h": "/v1/networks/*/chart", "/v1/charts/all/all/7d": "/v1/networks/all/chart",
		"/v1/networks/net00/chart?range=30d": "/v1/networks/*/chart", "/v1/targets/residential/24h/chrome": "/v1/targets",
		"/v1/places/city/AA%7CR0?range=30d": "/v1/places/*", "/v1/countries/AA/cities": "/v1/countries/*/cities",
		"/v1/rankings?range=99d": "/v1/rankings", "dataset file": "dataset file", "/v1/dataset": "dataset file",
	} {
		if got := englishGroup(name); got != want {
			t.Errorf("englishGroup(%q) = %q, want %q", name, got, want)
		}
	}
}
