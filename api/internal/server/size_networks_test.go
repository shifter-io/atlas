package server

import (
	"encoding/json"
	"net/http"
	"os"
	"runtime"
	"runtime/debug"
	"testing"

	"github.com/Zeus-Development/proxy-index-website/api/internal/build"
	"github.com/Zeus-Development/proxy-index-website/api/internal/store"
	"github.com/Zeus-Development/proxy-index-website/api/internal/testscale"
)

// TestEveryV4AnswerFitsTheCap is case 4 of API-SUM-1: the QA-1 build as
// payload v4 (18 networks, each one summary over 40 requested countries)
// gives every answer, and every page of its long lists, within
// build.MaxAnswerBytes, and its network figures are the summaries' own.
func TestEveryV4AnswerFitsTheCap(t *testing.T) {
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
	largest := 0
	for p, r := range v.Static {
		largest = max(largest, len(r.Identity))
		if len(r.Identity) > build.MaxAnswerBytes {
			t.Errorf("%s is %d bytes, above the %d-byte cap", p, len(r.Identity), build.MaxAnswerBytes)
		}
	}
	srv := New(&fakeSource{}, Options{})
	srv.current.Store(v)
	h := srv.Handler()
	for _, path := range []string{"/v1/networks/net00/places?dimension=city", "/v1/networks/net03/places?dimension=asn&range=7d&page=2",
		"/v1/networks/net17/places?dimension=country&range=24h", "/v1/countries/AA/cities", "/v1/networks/all/places?dimension=city&range=24h"} {
		rec := get(h, path)
		var page build.PlacePage
		if rec.Code != http.StatusOK || json.Unmarshal(rec.Body.Bytes(), &page) != nil || len(page.Rows) == 0 || rec.Body.Len() > build.MaxAnswerBytes {
			t.Errorf("%s: %d, %d bytes, %d rows", path, rec.Code, rec.Body.Len(), len(page.Rows))
		}
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
	if ans == nil || ans.ProductGroups != 1 || float64(ans.Ranges["30d"].Requests) != w.Metrics.Completed ||
		ans.Ranges["30d"].Lists["city"] != w.Lists.Entries["city"] {
		t.Errorf("net00's 30d answer is not its summary's: %+v, summary %+v", ans, w)
	}
	t.Logf("v4 QA-1: %d answers, largest %d bytes, dataset file %d bytes (gzip %d)", len(v.Static), largest, v.Dataset.Bytes, v.Dataset.Gzip)
}

// heap is the live heap after a collection.
func heap() uint64 {
	runtime.GC()
	var m runtime.MemStats
	runtime.ReadMemStats(&m)
	return m.HeapAlloc
}

// TestNetworkMemoryProbe logs (case 5 of API-SUM-1, not an assertion) the
// heap a QA-1-sized build holds: the v3 snapshot (QA1, its typed tails), and
// the v4 head with its compact network lists (QA1Networks), before and with
// the build's transient bridge (Full and Places). It runs only when asked
// (SITEAPI_MEMORY_PROBE=1), like TestPeakRSS: it is a measurement, not a
// check, and holds about 1 GB for about 25 s of the default suite on a 4 GB
// CI runner.
func TestNetworkMemoryProbe(t *testing.T) {
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
	held4 := heap() - base
	s := &store.SnapshotRow{BuildID: 1, Payload: head}
	if err := s.SetNetworks(lists); err != nil {
		t.Fatal(err)
	}
	bridged := heap() - base
	runtime.KeepAlive(s)
	runtime.KeepAlive(lists)
	t.Logf("memory probe: v3 QA1 inputs %.1f MB; v4 QA1Networks head %.1f MB and %d compact lists, %.1f MB together; "+
		"with the transient bridge (Full %.1f MB and its typed rows) %.1f MB", mb(held3), mb(uint64(len(head))), len(lists), mb(held4),
		mb(uint64(len(s.Full()))), mb(bridged))
}

func mb(n uint64) float64 { return float64(n) / (1 << 20) }
