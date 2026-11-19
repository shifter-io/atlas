package server_test

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"math/rand/v2"
	"net/http"
	"net/http/httptest"
	"net/url"
	"slices"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/shifter-io/atlas/api/internal/server"
	"github.com/shifter-io/atlas/api/internal/store"
	"github.com/shifter-io/atlas/api/internal/testdb"
	"github.com/shifter-io/atlas/api/internal/testscale"
)

// The flood test's shape (SEC-COST-1): the default limits of a 4-processor
// server as production's, flooded by floodClients for floodFor. The test's
// clients run in the same process, so it keeps every processor.
const (
	floodProcs   = 4
	floodClients = 64
	floodFor     = 3 * time.Second
	cachedP99    = 50 * time.Millisecond
)

// floodTally counts the flood's answers by kind.
type floodTally struct{ ok, busy, other atomic.Int64 }

// TestFloodKeepsCachedAnswersFast (SEC-COST-1): on testscale.Week, with the
// default limits of production's processors, floodClients clients send
// distinct uncached requests (made-up place keys, random list searches and
// pages, events cursors with a random search) for floodFor while a forced
// refresh runs. Cached answers keep their p99 under cachedP99, some flood
// requests answer 503 busy with Retry-After, none fails otherwise, and the
// refresh finishes.
func TestFloodKeepsCachedAnswersFast(t *testing.T) {
	// Serial and alone: api.yml runs it with the page latency test.
	db := testdb.New(t)
	if testing.Short() || raceEnabled {
		t.Skip("measures latency")
	}
	s := weekServer(t, db, testscale.Week)
	server.SetUncachedLimit(s, server.UncachedLimitFor(floodProcs), store.DefaultRequestConns)
	ts := httptest.NewServer(s.Handler())
	defer ts.Close()
	_, slugs := weekShape(t, s)
	ctx, stop := context.WithTimeout(context.Background(), floodFor)
	defer stop()
	refreshed := make(chan error, 1)
	start := time.Now()
	go func() {
		_, err := s.Refresh(context.Background(), true)
		refreshed <- err
	}()
	var tally floodTally
	var wg sync.WaitGroup
	client := &http.Client{Transport: &http.Transport{MaxIdleConnsPerHost: floodClients}}
	for i := range floodClients {
		wg.Go(func() { flood(ctx, t, client, ts.URL, slugs, uint64(i), &tally) })
	}
	cached := measureCached(ctx, t, ts.URL, slugs)
	wg.Wait()
	if err := <-refreshed; err != nil {
		t.Errorf("the forced refresh: %v", err)
	}
	took := time.Since(start)
	slices.Sort(cached)
	p99 := cached[len(cached)*99/100]
	t.Logf("FLOOD %d clients %v: %d computed, %d busy, %d other; cached p99 %v of %d; refresh done after %v",
		floodClients, floodFor, tally.ok.Load(), tally.busy.Load(), tally.other.Load(), p99, len(cached), took.Round(time.Millisecond))
	if p99 > cachedP99 || tally.busy.Load() == 0 || tally.other.Load() != 0 || tally.ok.Load() == 0 {
		t.Errorf("cached p99 %v (want under %v), %d busy (want some), %d computed, %d other (want none)",
			p99, cachedP99, tally.busy.Load(), tally.ok.Load(), tally.other.Load())
	}
}

// flood sends distinct uncached requests until ctx ends: 200 and 404 (a
// made-up place) or 400 (a cursor that names no event) count as computed,
// 503 busy with Retry-After as busy, anything else as other.
func flood(ctx context.Context, t *testing.T, c *http.Client, base string, slugs []string, seed uint64, tally *floodTally) {
	rnd := rand.New(rand.NewPCG(seed, 1018))
	word := func() string { return fmt.Sprintf("%c%c", 'a'+rnd.IntN(26), 'a'+rnd.IntN(26)) }
	for n := 0; ctx.Err() == nil; n++ {
		var path string
		switch n % 4 {
		case 0:
			path = fmt.Sprintf("/v1/places/city/AA%%7CR%d%%7Cmade%d?range=30d", rnd.IntN(1e6), n)
		case 1:
			path = fmt.Sprintf("/v1/networks/%s/places?range=30d&dimension=city&q=%s", slugs[rnd.IntN(len(slugs))], word())
		case 2:
			path = fmt.Sprintf("/v1/countries/AA/cities?range=7d&page=%d&q=%s", 1+rnd.IntN(5), word())
		default:
			path = fmt.Sprintf("/v1/events?before=%d&q=%s", 1+rnd.IntN(testscale.Week.Events), word())
		}
		req, _ := http.NewRequestWithContext(ctx, http.MethodGet, base+path, nil)
		resp, err := c.Do(req)
		if err != nil {
			if ctx.Err() == nil {
				t.Errorf("%s: %v", path, err)
			}
			return
		}
		body, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		switch code := resp.StatusCode; {
		case code == http.StatusOK || code == http.StatusNotFound || code == http.StatusBadRequest:
			tally.ok.Add(1)
		case code == http.StatusServiceUnavailable && resp.Header.Get("Retry-After") == "1" && busyBody(body):
			tally.busy.Add(1)
		default:
			tally.other.Add(1)
			t.Errorf("%s: %d %s", path, code, body)
		}
	}
}

func busyBody(b []byte) bool {
	var e struct{ Error struct{ Code string } }
	return json.Unmarshal(b, &e) == nil && e.Error.Code == "busy"
}

// measureCached asks for cached answers (precomputed bodies) one after
// another until ctx ends and returns each one's time.
func measureCached(ctx context.Context, t *testing.T, base string, slugs []string) []time.Duration {
	c := &http.Client{Transport: &http.Transport{MaxIdleConnsPerHost: 1}}
	paths := []string{"/v1/networks", "/v1/countries", "/v1/rankings?range=30d", "/v1/networks/" + slugs[0], "/v1/countries/AA"}
	out := []time.Duration{}
	for n := 0; ctx.Err() == nil; n++ {
		start := time.Now()
		resp, err := c.Get(base + paths[n%len(paths)])
		if err != nil {
			t.Fatal(err)
		}
		io.Copy(io.Discard, resp.Body) //nolint:errcheck
		resp.Body.Close()
		if resp.StatusCode != http.StatusOK {
			t.Errorf("cached %s: %d", paths[n%len(paths)], resp.StatusCode)
		}
		out = append(out, time.Since(start))
	}
	return out
}

// SEC-COST-1: with no list rows in memory or in the database, a made-up place
// key still answers 404 (no row read), where a place a list holds fails
// reading its rows.
func TestAMadeUpPlaceReadsNoRowFromTheDatabase(t *testing.T) {
	db := testdb.New(t)
	if testing.Short() {
		t.Skip("publishes a build")
	}
	shape := testscale.Week
	shape.Networks, shape.Regions, shape.Cities, shape.Asns, shape.CityAsns, shape.Events = 2, 20, 60, 20, 60, 10
	s := weekServerWith(t, db, shape, testdb.V4Lists{Requested: shape.Requested, Rows: shape.Lists()}, &store.Lists{RowCacheBytes: 1})
	ts := httptest.NewServer(s.Handler())
	defer ts.Close()
	var page struct{ Rows []struct{ Key string } }
	if code := getJSON(t, ts.URL+"/v1/countries/AA/cities?range=30d", &page); code != http.StatusOK || len(page.Rows) == 0 {
		t.Fatalf("cities: %d %+v", code, page)
	}
	key := page.Rows[0].Key
	db.Exec(t, `DELETE FROM calculated_list_rows`)
	place := func(k string) string { return ts.URL + "/v1/places/city/" + url.PathEscape(k) + "?range=30d" }
	if code := status(t, place(key)); code != http.StatusInternalServerError {
		t.Errorf("held city %s without its rows: %d, want 500 (its rows are read)", key, code)
	}
	for _, k := range []string{key + "x", "AA|made|up", "ZZ|" + key} {
		if code := status(t, place(k)); code != http.StatusNotFound {
			t.Errorf("made-up city %s: %d, want 404 without a read", k, code)
		}
	}
}

func getJSON(t *testing.T, u string, v any) int {
	t.Helper()
	resp, err := http.Get(u)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	json.NewDecoder(resp.Body).Decode(v) //nolint:errcheck
	return resp.StatusCode
}
