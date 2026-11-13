package server_test

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/server"
	"github.com/Zeus-Development/proxy-index/api/internal/store"
	"github.com/Zeus-Development/proxy-index/api/internal/testdb"
	"github.com/Zeus-Development/proxy-index/api/internal/testscale"
)

// With a row cache too small for a build's lists, the warm step keeps each
// list's smallest dimension (its regions) and leaves the largest out
// (ROW-CACHE-1): once the rows are deleted, every network window answers a
// q= search of a warm list and a cold one's answers 500. /healthz coldLists
// counts exactly the cold ones, the largest a City x ASN list.
func TestHealthNamesTheColdLists(t *testing.T) {
	db := testdb.New(t)
	if testing.Short() {
		t.Skip("publishes a build")
	}
	shape := testscale.Week
	shape.Networks, shape.Regions, shape.Cities, shape.Asns, shape.CityAsns, shape.Events = 2, 100, 300, 200, 400, 10
	// Every list's 100 regions and four lists' 200 ASNs.
	limit := (2*3*100 + 4*200) * 28
	s := weekServerWith(t, db, shape, testdb.V4Lists{Requested: shape.Requested, Rows: shape.Lists()}, &store.Lists{RowCacheBytes: limit})
	db.Exec(t, `DELETE FROM calculated_list_rows`)
	ts := httptest.NewServer(s.Handler())
	defer ts.Close()
	cold := 0
	for _, n := range s.Current().Result.Pages.Networks.Networks {
		for _, r := range weekRanges {
			warm := 0
			for _, path := range windowSearches(n.Slug, r) {
				switch code := status(t, ts.URL+path); code {
				case http.StatusOK:
					warm++
				case http.StatusInternalServerError:
					cold++
				default:
					t.Errorf("%s: %d", path, code)
				}
			}
			if warm == 0 {
				t.Errorf("%s %s: every list cold", n.Slug, r)
			}
		}
	}
	admin := httptest.NewServer(s.AdminHandler())
	defer admin.Close()
	h := health(t, admin.URL)
	c := h.ColdLists
	if c.Count == 0 || c.Count != cold || c.Largest == nil || c.Largest.Dimension != "city_asn" || c.Largest.Rows != 400 || h.Status != "ok" {
		t.Errorf("/healthz %s coldLists %+v (largest %+v), want %d cold searches' lists, the largest 400 City x ASN rows", h.Status, c, c.Largest, cold)
	}
}

// windowSearches is a q= search of each list of a network window: its place
// lists and its City x ASN coverage.
func windowSearches(slug, rangeID string) []string {
	out := []string{fmt.Sprintf("/v1/networks/%s/coverage?range=%s&q=city", slug, rangeID)}
	for _, d := range []string{"country", "region", "city", "asn"} {
		out = append(out, fmt.Sprintf("/v1/networks/%s/places?range=%s&dimension=%s&q=a", slug, rangeID, d))
	}
	return out
}

func status(t *testing.T, url string) int {
	t.Helper()
	resp, err := http.Get(url)
	if err != nil {
		t.Fatal(err)
	}
	io.Copy(io.Discard, resp.Body) //nolint:errcheck
	resp.Body.Close()
	return resp.StatusCode
}

// health is base's /healthz (an admin listener: the full Health).
func health(t *testing.T, base string) server.Health {
	t.Helper()
	var h server.Health
	resp, err := http.Get(base + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if err := json.NewDecoder(resp.Body).Decode(&h); err != nil {
		t.Fatal(err)
	}
	return h
}
