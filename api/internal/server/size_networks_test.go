package server

import (
	"context"
	"encoding/json"
	"net/http"
	"net/url"
	"os"
	"runtime"
	"runtime/debug"
	"strings"
	"testing"

	"github.com/Zeus-Development/atlas/api/internal/build"
	"github.com/Zeus-Development/atlas/api/internal/store"
	"github.com/Zeus-Development/atlas/api/internal/testscale"
)

// TestEveryV4AnswerFitsTheCap is case 4 of API-SUM-1 and case 6 of
// API-COVERAGE-1: the QA-1 build as payload v4 (18 networks, each one summary
// over 40 requested countries) gives every answer, and pages of its place,
// Coverage, region and place page answers, within build.MaxAnswerBytes, and
// its network figures are the summaries' own.
func TestEveryV4AnswerFitsTheCap(t *testing.T) {
	t.Parallel()
	if testing.Short() || underRace {
		t.Skip("renders a QA-1-sized build (see scaleVersion)")
	}
	defer debug.FreeOSMemory()
	in, err := testscale.NetworkInputs(testscale.QA1Networks)
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
	largest, sizes := 0, map[string]int{}
	for p, r := range v.Static {
		largest = max(largest, len(r.Identity))
		if len(r.Identity) > build.MaxAnswerBytes {
			t.Errorf("%s is %d bytes, above the %d-byte cap", p, len(r.Identity), build.MaxAnswerBytes)
		}
	}
	srv := New(&fakeSource{}, Options{})
	srv.current.Store(v)
	h := srv.Handler()
	city := res.Pages.Network["net00/residential"].Ranges["30d"].Countries[0].Key + "|"
	if p, ok, _ := res.Pages.List(context.Background(), "", "country/"+city[:2], "30d", "city", "", 1); ok && len(p.Rows) > 0 {
		city = p.Rows[0].Key
	}
	for _, path := range []string{"/v1/networks/net00/places?dimension=city", "/v1/networks/net03/places?dimension=asn&range=7d&page=2",
		"/v1/networks/net17/places?dimension=country&range=24h", "/v1/countries/AA/cities", "/v1/networks/all/places?dimension=city&range=24h",
		"/v1/networks/net00/coverage", "/v1/networks/net05/coverage?range=7d&page=3", "/v1/networks/net09/coverage?q=aa&range=24h",
		"/v1/countries/AA/regions", "/v1/countries/AB/regions?range=7d", "/v1/places/city/" + url.PathEscape(city),
		"/v1/places/region/" + url.PathEscape(city[:strings.LastIndexByte(city, '|')])} {
		rec := get(h, path)
		var page struct{ Rows []json.RawMessage }
		if rec.Code != http.StatusOK || json.Unmarshal(rec.Body.Bytes(), &page) != nil || len(page.Rows) == 0 || rec.Body.Len() > build.MaxAnswerBytes {
			t.Errorf("%s: %d, %d bytes, %d rows", path, rec.Code, rec.Body.Len(), len(page.Rows))
		}
		sizes[path] = rec.Body.Len()
	}
	var head struct {
		Networks []struct {
			Windows map[string]struct {
				Metrics struct {
					Completed float64 `json:"completed"`
				} `json:"metrics"`
				Lists struct {
					Entries map[string]int `json:"entries"`
				} `json:"lists"`
			} `json:"windows"`
		} `json:"networks"`
	}
	if err := json.Unmarshal(in.Snapshot.Payload, &head); err != nil || len(head.Networks) != 18 {
		t.Fatalf("%d networks (%v)", len(head.Networks), err)
	}
	ans, w := res.Pages.Network["net00/residential"], head.Networks[0].Windows["30d"]
	if ans == nil || ans.ProductGroups != 40 || len(ans.RequestedCountries) != 40 || float64(ans.Ranges["30d"].Requests) != w.Metrics.Completed ||
		ans.Ranges["30d"].Lists["city"] != w.Lists.Entries["city"] {
		t.Errorf("net00's 30d answer is not its summary's: %+v, summary %+v", ans, w)
	}
	t.Logf("v4 QA-1: %d answers, largest %d bytes, dataset file %d bytes (gzip %d); bytes of the new answers: %v",
		len(v.Static), largest, v.Dataset.Bytes, v.Dataset.Gzip, sizes)
}

// heap is the live heap after a collection.
func heap() uint64 {
	runtime.GC()
	var m runtime.MemStats
	runtime.ReadMemStats(&m)
	return m.HeapAlloc
}

// TestNetworkMemoryProbe is case 5 of API-COVERAGE-1 (logged by API-SUM-1):
// the heap a QA-1-sized build holds, the v3 snapshot (QA1, its typed tails)
// beside the v4 head with its compact network lists (QA1Networks) and the
// payload the build reads (Full), which must stay within 64 MB of the head
// and the lists: no typed row is held. Before API-COVERAGE-1, that payload
// with the typed rows of every list held 111 MB (the bridge). It also logs
// the heap with the load and the v4 build's Result. It runs only when asked
// (SITEAPI_MEMORY_PROBE=1), like TestPeakRSS: it holds about 1 GB for about
// 25 s of the default suite on a 4 GB CI runner.
func TestNetworkMemoryProbe(t *testing.T) {
	// Serial: it measures the whole process's heap.
	if os.Getenv("SITEAPI_MEMORY_PROBE") == "" {
		t.Skip("set SITEAPI_MEMORY_PROBE=1 to log the heap of a QA-1-sized v3 and v4 snapshot")
	}
	if testing.Short() || underRace {
		t.Skip("builds QA-1-sized snapshots (see scaleVersion)")
	}
	defer debug.FreeOSMemory()
	base := heap()
	v3, err := testscale.Inputs(testscale.QA1)
	if err != nil {
		t.Fatal(err)
	}
	held3 := heap() - base
	runtime.KeepAlive(v3)
	base = heap() // the typed tails stay cached (tailLists): v4 shares them
	in, err := testscale.Inputs(testscale.QA1Networks.Shape)
	if err != nil {
		t.Fatal(err)
	}
	head, lists, err := testscale.Networks(in.Snapshot, testscale.QA1Networks.Requested)
	if err != nil {
		t.Fatal(err)
	}
	in.Snapshot = nil // the v3 snapshot the v4 one was made from
	held4 := heap() - base
	s := &store.SnapshotRow{BuildID: 1, Payload: head}
	if err := s.SetNetworks(lists); err != nil {
		t.Fatal(err)
	}
	loaded := heap() - base
	full := len(s.Full())
	in.Snapshot = s
	res, err := build.Build(in)
	if err != nil {
		t.Fatal(err)
	}
	built := heap() - base
	runtime.KeepAlive(res)
	runtime.KeepAlive(lists)
	t.Logf("memory probe: v3 QA1 inputs %.1f MB; v4 QA1Networks head %.1f MB and %d compact lists, %.1f MB together; "+
		"after the load (Full %.1f MB, no typed rows) %.1f MB; with the load and the built Result %.1f MB", mb(held3), mb(uint64(len(head))), len(lists),
		mb(held4), mb(uint64(full)), mb(loaded), mb(built))
	if s.Places != nil || loaded > held4+64<<20 {
		t.Errorf("a v4 load holds %.1f MB, above the head and lists (%.1f MB) plus 64 MB, or typed rows (%d)", mb(loaded), mb(held4), len(s.Places))
	}
}

func mb(n uint64) float64 { return float64(n) / (1 << 20) }
