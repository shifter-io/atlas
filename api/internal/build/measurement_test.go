package build

import (
	"encoding/json"
	"testing"

	"github.com/shifter-io/atlas/api/internal/snap"
)

// STANDING-FREEZE-1: a network window's standing is served as its
// measurementState.

func standingProduct(standing *snap.WindowStanding) *snap.Product {
	return &snap.Product{Windows: map[string]snap.Window{"24h": {Standing: standing}}}
}

func TestMeasurementServesTheWindowStanding(t *testing.T) {
	last, from := "2026-11-19T13:10:00+00:00", "2026-11-19T13:00:00+00:00"
	frozen := &snap.WindowStanding{Status: snap.StandingFrozen, LastMeasured: &last, FrozenSince: &last, ValuesFrom: &from}
	got, _ := json.Marshal(measurement(standingProduct(frozen), "24h"))
	want := `{"status":"frozen","lastMeasuredAt":"2026-11-19T13:10:00.000Z","frozenSince":"2026-11-19T13:10:00.000Z",` +
		`"valuesAsOf":"2026-11-19T13:00:00.000Z"}`
	if string(got) != want {
		t.Fatalf("frozen: %s, want %s", got, want)
	}
	none := `{"status":null,"lastMeasuredAt":null,"frozenSince":null,"valuesAsOf":null}`
	for name, sp := range map[string]*snap.Product{"no product": nil, "older snapshot": standingProduct(nil)} {
		if got, _ := json.Marshal(measurement(sp, "24h")); string(got) != none {
			t.Errorf("%s: %s, want %s", name, got, none)
		}
	}
	measured := &snap.WindowStanding{Status: snap.StandingMeasured, LastMeasured: &last}
	if standingProduct(measured).Window("24h").Held() || !standingProduct(frozen).Window("24h").Held() {
		t.Error("only a frozen or stopped window is held")
	}
}
