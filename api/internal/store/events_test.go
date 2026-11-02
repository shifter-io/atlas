package store_test

import (
	"context"
	"testing"

	"github.com/Zeus-Development/proxy-index-website/api/internal/store"
	"github.com/Zeus-Development/proxy-index-website/api/internal/testdb"
)

// tapeKinds is every kind the framework writes, with its live tape tab
// (proxy-index detect.category).
var tapeKinds = map[string]string{
	"census_started": "census", "census_finished": "census", "census_retry": "census", "census_skipped": "census",
	"daily_test_started": "census", "daily_test_finished": "census", "estimate_hour_released": "census",
	"pulse_finished": "pulse", "pulse_down": "pulse", "pulse_recovered": "pulse",
	"success_drop": "incident", "latency_spike": "incident", "latency_rise": "incident", "block_spike": "incident",
	"down": "incident", "recovered": "incident", "node_offline": "incident", "no_node": "incident",
	"new_hosting_asn": "audit", "ip_overlap": "audit",
}

func TestCategoryOfEveryKind(t *testing.T) {
	for kind, want := range tapeKinds {
		if got := store.Category(kind); got != want {
			t.Errorf("Category(%q) = %q, want %q", kind, got, want)
		}
	}
}

// The database filter of /v1/events must put each kind on the same tab as Category.
func TestQueryEventsCategoryMatchesCategory(t *testing.T) {
	db := testdb.New(t)
	ctx := context.Background()
	for kind := range tapeKinds {
		db.Exec(t, "INSERT INTO test_events(at,kind,signal,severity) VALUES ('2026-11-02T12:00:00Z',$1,0,'info')", kind)
	}
	c := db.Conn(t)
	defer c.Close(ctx)
	for _, tab := range []string{"census", "pulse", "incident", "audit"} {
		rows, err := store.QueryEvents(ctx, c, store.EventFilter{Category: tab, Limit: 100})
		if err != nil {
			t.Fatal(err)
		}
		got := map[string]bool{}
		for _, e := range rows {
			got[e.Kind] = true
			if store.Category(e.Kind) != tab {
				t.Errorf("%s listed under %s, Category says %s", e.Kind, tab, store.Category(e.Kind))
			}
		}
		for kind, want := range tapeKinds {
			if want == tab && !got[kind] {
				t.Errorf("%s missing from the %s tab", kind, tab)
			}
		}
	}
}
