package server

import (
	"encoding/json"
	"net/http"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/build"
	"github.com/Zeus-Development/proxy-index/api/internal/testscale"
)

// TestAV4BuildServesCoverageFromTheCompactLists is regression case 8 of
// API-COVERAGE-1. It uses only what the base already had, so it runs on
// both: on the base a v4 load holds the typed rows of every list and a
// network has no Coverage answer (404); after the change the load holds no
// typed row and the answer pages the network's City x ASN rows.
func TestAV4BuildServesCoverageFromTheCompactLists(t *testing.T) {
	t.Parallel()
	smallV4(t)
}

// smallV4 serves a v4 build of two networks, each a summary over two
// requested countries, and checks its Coverage answer.
func smallV4(t *testing.T) (http.Handler, *build.Result) {
	t.Helper()
	in, err := testscale.NetworkInputs(testscale.NetworkShape{Shape: testscale.Shape{Networks: 2, Countries: 1, Exits: 3, Tail: 30,
		CityAsn: 60}, Requested: 2})
	if err != nil {
		t.Fatal(err)
	}
	if n := len(in.Snapshot.Places); n != 0 {
		t.Errorf("a v4 load holds the typed rows of %d lists", n)
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
	rec := get(srv.Handler(), "/v1/networks/net00/coverage?range=7d&page=2")
	var page struct {
		Total int               `json:"total"`
		Rows  []json.RawMessage `json:"rows"`
	}
	if rec.Code != http.StatusOK || json.Unmarshal(rec.Body.Bytes(), &page) != nil || page.Total == 0 || len(page.Rows) == 0 {
		t.Errorf("coverage: %d %.200s", rec.Code, rec.Body.String())
	}
	return srv.Handler(), res
}
