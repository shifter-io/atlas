package server

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"

	"github.com/shifter-io/atlas/api/internal/build"
	"golang.org/x/sync/semaphore"
)

// A region or city page answers every network's row in that place; a bad
// dimension, range, key or parameter is 400, an unknown key 404, and nothing
// is served before the first build.
func TestPlacePagesAnswerAndRefuse(t *testing.T) {
	t.Parallel()
	h, res := smallV4(t)
	cities, _, _ := res.Pages.List(context.Background(), "", "net00/residential", "30d", "city", build.ListFilter{}, 1)
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
		"/v1/networks/net00/coverage?country=aa&q=city":             http.StatusOK,
		"/v1/networks/net00/places?country=AA&dimension=city":       http.StatusOK,
		"/v1/networks/net00/coverage?country=":                      http.StatusBadRequest, // a two-letter code
		"/v1/networks/net00/places?country=USA":                     http.StatusBadRequest,
		"/v1/networks/net00/places?country=1A":                      http.StatusBadRequest,
		"/v1/countries/AA/cities?country=AA":                        http.StatusBadRequest, // only a network's lists
	} {
		for range 2 { // the second answer comes from the page LRU
			if rec := get(h, path); rec.Code != code {
				t.Errorf("%s: %d, want %d: %.200s", path, rec.Code, code, rec.Body.String())
			}
		}
	}
	var cov struct { // country= is case-insensitive and keeps that country's rows
		Total   int      `json:"total"`
		Columns []string `json:"columns"`
		Rows    [][]any  `json:"rows"`
	}
	if rec := get(h, "/v1/networks/net00/coverage?country=aa"); json.Unmarshal(rec.Body.Bytes(), &cov) != nil || cov.Total == 0 ||
		len(cov.Columns) != 12 || cov.Columns[0] != "countryCode" || cov.Columns[1] != "region" || cov.Rows[len(cov.Rows)-1][0] != "AA" ||
		len(cov.Rows[0]) != 12 {
		t.Errorf("coverage of AA: %.300s", rec.Body.String())
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

// A page whose list rows cannot be read (the database is gone, or the list
// was deleted) answers 500 and is logged, and nothing is kept for it.
func TestAPageWhoseRowsCannotBeReadIs500(t *testing.T) {
	var log bytes.Buffer
	s := &Server{log: slog.New(slog.NewTextHandler(&log, nil)), work: semaphore.NewWeighted(1)}
	v := &Version{pages: NewLRU(4)}
	rec := httptest.NewRecorder()
	s.serveCut(rec, httptest.NewRequest(http.MethodGet, "/v1/networks/net00/places?page=2", nil), v, "key", func() (any, bool, error) {
		return nil, false, errors.New("network list 7: closed pool")
	})
	if _, kept := v.pages.Get("key"); rec.Code != http.StatusInternalServerError || kept || !strings.Contains(log.String(), "network list 7: closed pool") {
		t.Errorf("%d %s, kept %v, log %q", rec.Code, rec.Body, kept, log.String())
	}
}
