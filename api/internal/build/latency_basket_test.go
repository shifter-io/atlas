package build

import (
	"testing"

	"github.com/shifter-io/atlas/api/internal/model"
	"github.com/shifter-io/atlas/api/internal/snap"
)

// basketProduct is a measured product whose 7d window's generic_http
// latency has the calculator's country basket (LATENCY-BASKET-1) when
// basket is its JSON ("" for a snapshot from before it).
func basketProduct(t *testing.T, basket string) *entry {
	t.Helper()
	m := `{"completed":10,"successes":10,"latency_ms":{"p50":633,"p90":900,"p99":1200}`
	if basket != "" {
		m += `,"latency_basket":` + basket
	}
	w := `{"period":{"start":"2026-11-12T12:00:00Z","end":"2026-11-19T12:00:00Z"},"targets":{"generic_http":{"metrics":` + m + `}}}}`
	sp := decodeProduct(t, `{"id":"p","windows":{"7d":`+w+`}}`)
	return &entry{P: &model.Provider{}, Snap: map[string]*snap.Product{"residential": sp}}
}

func TestTheBasketCoverageIsServedBesideTheLatency(t *testing.T) {
	t.Parallel()
	e := basketProduct(t, `{"size":38,"covered":30,"counted":37,"unserved":["RU"],"pooled_ms":{"p50":348}}`)
	ws := windowStats(e.P, e.Snap["residential"], "7d", nil)
	if ws.LatencyP50 != 633 || ws.LatencyBasketCovered != 30 || ws.LatencyBasketSize != 38 {
		t.Errorf("latency %v, basket %v of %v", ws.LatencyP50, ws.LatencyBasketCovered, ws.LatencyBasketSize)
	}
	// A snapshot calculated without the basket serves both null.
	old := basketProduct(t, "")
	ws = windowStats(old.P, old.Snap["residential"], "7d", nil)
	if ws.LatencyP50 != 633 || ws.LatencyBasketCovered.OK() || ws.LatencyBasketSize.OK() {
		t.Errorf("old snapshot: latency %v, basket %v of %v", ws.LatencyP50, ws.LatencyBasketCovered, ws.LatencyBasketSize)
	}
	// So does a provider without a snapshot product.
	ws = windowStats(&model.Provider{}, nil, "7d", nil)
	if ws.LatencyBasketCovered.OK() || ws.LatencyBasketSize.OK() {
		t.Errorf("published provider: basket %v of %v", ws.LatencyBasketCovered, ws.LatencyBasketSize)
	}
}
