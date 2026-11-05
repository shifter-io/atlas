package server

import (
	"encoding/json"
	"net/http"
	"net/url"
	"strings"
	"testing"

	"github.com/Zeus-Development/proxy-index-website/api/internal/build"
)

// A region or city page answers every network's row in that place; a bad
// dimension, range, key or parameter is 400, an unknown key 404, and nothing
// is served before the first build.
func TestPlacePagesAnswerAndRefuse(t *testing.T) {
	h, res := smallV4(t)
	cities, _ := res.Pages.List("", "net00/residential", "30d", "city", "", 1)
	city := cities.Rows[0].Key
	region := city[:strings.LastIndexByte(city, '|')]
	for path, code := range map[string]int{
		"/v1/places/city/" + url.PathEscape(city):                   http.StatusOK,
		"/v1/places/region/" + url.PathEscape(region) + "?range=7d": http.StatusOK,
		"/v1/places/city/" + url.PathEscape(city+"x"):               http.StatusNotFound,
		"/v1/places/street/" + url.PathEscape(city):                 http.StatusBadRequest,
		"/v1/places/city/" + url.PathEscape(city) + "?range=1y":     http.StatusBadRequest,
		"/v1/places/city/" + strings.Repeat("a", 401):               http.StatusBadRequest,
		"/v1/places/city/" + url.PathEscape(city) + "?page=2":       http.StatusBadRequest,
		"/v1/networks/net00/places?dimension=coverage":              http.StatusNotFound, // review N2: only .../coverage
		"/v1/networks/net00/coverage?dimension=city":                http.StatusBadRequest,
	} {
		for range 2 { // the second answer comes from the page LRU
			if rec := get(h, path); rec.Code != code {
				t.Errorf("%s: %d, want %d: %.200s", path, rec.Code, code, rec.Body.String())
			}
		}
	}
	var page build.PlaceAnswer
	if rec := get(h, "/v1/places/city/"+url.PathEscape(city)); json.Unmarshal(rec.Body.Bytes(), &page) != nil || page.Key != city ||
		len(page.Rows) == 0 || page.Rows[0].IpsSumRank == nil {
		t.Errorf("city page %+v", page)
	}
	if rec := get(New(&fakeSource{}, Options{}).Handler(), "/v1/places/city/"+url.PathEscape(city)); rec.Code != http.StatusServiceUnavailable {
		t.Errorf("before the first build: %d", rec.Code)
	}
}
