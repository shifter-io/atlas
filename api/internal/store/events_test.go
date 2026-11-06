package store_test

import (
	"context"
	"slices"
	"testing"
	"time"

	"github.com/Zeus-Development/proxy-index/api/internal/store"
	"github.com/Zeus-Development/proxy-index/api/internal/testdb"
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

func kindsOf(rows []store.Event) []string {
	out := []string{}
	for _, e := range rows {
		out = append(out, e.Kind)
	}
	return out
}

func TestRoutineKinds(t *testing.T) {
	for kind, tab := range tapeKinds {
		want := tab == "census" || kind == "pulse_finished"
		if store.Routine(kind) != want {
			t.Errorf("Routine(%q) = %v", kind, !want)
		}
	}
}

// Prioritize keeps the newest notable rows up to half the page, then fills it
// with the newest rows of any kind, newest first.
func TestPrioritize(t *testing.T) {
	kinds := []string{"estimate_hour_released", "pulse_finished", "estimate_hour_released", "pulse_down",
		"latency_rise", "estimate_hour_released", "down", "recovered", "new_hosting_asn"}
	kind := func(i int) string { return kinds[i] }
	for _, c := range []struct {
		limit int
		want  []int
	}{
		{1, []int{3}},
		{2, []int{0, 3}},
		{4, []int{0, 1, 3, 4}},
		{6, []int{0, 1, 2, 3, 4, 6}},
		{9, []int{0, 1, 2, 3, 4, 5, 6, 7, 8}},
		{20, []int{0, 1, 2, 3, 4, 5, 6, 7, 8}},
	} {
		if got := store.Prioritize(len(kinds), c.limit, kind); !slices.Equal(got, c.want) {
			t.Errorf("limit %d: %v, want %v", c.limit, got, c.want)
		}
	}
	if got := store.Prioritize(0, 5, kind); len(got) != 0 {
		t.Errorf("no rows: %v", got)
	}
}

// A named kind or tab gets exactly the newest rows; the tape's filters get the choice.
func TestPickPrioritizesOnlyUnnamedFilters(t *testing.T) {
	at := time.Date(2026, 11, 2, 16, 0, 0, 0, time.UTC)
	rows := []store.Event{}
	for i, k := range []string{"estimate_hour_released", "estimate_hour_released", "estimate_hour_released", "pulse_down"} {
		rows = append(rows, store.Event{ID: int64(10 - i), At: at.Add(-time.Duration(i) * time.Minute), Kind: k})
	}
	if got := kindsOf(store.Pick(rows, store.EventFilter{Limit: 2})); !slices.Equal(got, []string{"estimate_hour_released", "pulse_down"}) {
		t.Errorf("tape filter: %v", got)
	}
	if got := store.Pick(rows, store.EventFilter{Category: "census", Limit: 2}); len(got) != 2 || got[1].ID != 9 {
		t.Errorf("census tab: %v", kindsOf(got))
	}
	if got := store.Pick(rows, store.EventFilter{Kinds: []string{"estimate_hour_released", "pulse_down"}, Limit: 2}); got[1].ID != 9 {
		t.Errorf("named kinds: %v", kindsOf(got))
	}
}

// TAPE-2 against the database: a release burst bigger than the in-memory window
// leaves the older pulse down and latency rise in memory (Inputs.Notable) and on
// the database fallback's first page.
func TestNotableEventsSurviveAReleaseBurst(t *testing.T) {
	db := testdb.New(t)
	ctx := context.Background()
	db.Exec(t, "INSERT INTO test_events(at,kind,signal,severity) VALUES ('2026-11-02T15:00:00Z','pulse_down',-1,'major'),"+
		"('2026-11-02T14:00:00Z','latency_rise',-1,'minor'),('2026-11-02T13:00:00Z','estimate_hour_released',0,'info')")
	db.Exec(t, "INSERT INTO test_events(at,kind,signal,severity) SELECT '2026-11-02T16:00:30Z','estimate_hour_released',0,'info'"+
		" FROM generate_series(1,$1::int)", store.EventWindow+1)
	pool, err := store.OpenReadOnly(ctx, db.DSN)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	in, err := store.Load(ctx, pool)
	if err != nil {
		t.Fatal(err)
	}
	if len(in.Events) != store.EventWindow || in.EventsComplete || !in.NotableComplete ||
		!slices.Equal(kindsOf(in.Notable), []string{"pulse_down", "latency_rise"}) {
		t.Fatalf("window %d complete %v, notable %v complete %v", len(in.Events), in.EventsComplete, kindsOf(in.Notable), in.NotableComplete)
	}
	page, err := store.QueryEvents(ctx, pool, store.EventFilter{Limit: 4})
	if err != nil {
		t.Fatal(err)
	}
	if got := kindsOf(page); !slices.Equal(got, []string{"estimate_hour_released", "estimate_hour_released", "pulse_down", "latency_rise"}) {
		t.Fatalf("first page %v", got)
	}
	tab, err := store.QueryEvents(ctx, pool, store.EventFilter{Category: "incident", Limit: 4})
	if err != nil || !slices.Equal(kindsOf(tab), []string{"latency_rise"}) {
		t.Fatalf("incident tab %v %v", kindsOf(tab), err)
	}
	since := time.Date(2026, 11, 2, 14, 30, 0, 0, time.UTC)
	named, err := store.QueryEvents(ctx, pool, store.EventFilter{Kinds: []string{"pulse_down", "latency_rise"}, Since: &since, Limit: 4})
	if err != nil || !slices.Equal(kindsOf(named), []string{"pulse_down"}) {
		t.Fatalf("named kinds since 14:30 %v %v", kindsOf(named), err)
	}
	if none, err := store.QueryEvents(ctx, pool, store.EventFilter{Product: "nobody/residential", Limit: 4}); err != nil || len(none) != 0 {
		t.Fatalf("unknown product %v %v", kindsOf(none), err)
	}
}

// Two lists held newest first merge into one, a row in both kept once.
func TestMergeNewest(t *testing.T) {
	at := time.Date(2026, 11, 2, 16, 0, 0, 0, time.UTC)
	row := func(id int64, minutes int) store.Event {
		return store.Event{ID: id, At: at.Add(time.Duration(minutes) * time.Minute)}
	}
	a := []store.Event{row(9, 5), row(7, 0), row(6, 0), row(2, -9)}
	b := []store.Event{row(8, 3), row(7, 0), row(3, -5)}
	ids := []int64{}
	for _, e := range store.MergeNewest(a, b) {
		ids = append(ids, e.ID)
	}
	if !slices.Equal(ids, []int64{9, 8, 7, 6, 3, 2}) {
		t.Fatalf("merged %v", ids)
	}
}

// LoadEvents (an events-only refresh) reads the events exactly as Load does,
// and the newest event id the fingerprint has: none on an empty table, then a
// cut window with notable rows past it.
func TestLoadEventsReadsTheEventsLoadReads(t *testing.T) {
	db := testdb.New(t)
	ctx := context.Background()
	pool, err := store.OpenReadOnly(ctx, db.DSN)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	same := func(stage string) {
		t.Helper()
		in, err := store.Load(ctx, pool)
		if err != nil {
			t.Fatal(err)
		}
		ev, err := store.LoadEvents(ctx, pool)
		if err != nil {
			t.Fatal(err)
		}
		fp, err := store.ReadFingerprint(ctx, pool)
		if err != nil {
			t.Fatal(err)
		}
		if ev.Fingerprint.EventsKey() != in.Fingerprint.EventsKey() || ev.Fingerprint.EventsKey() != fp.EventsKey() ||
			in.Fingerprint.DataKey() != fp.DataKey() {
			t.Fatalf("%s: events key %q, load %q, fingerprint %q", stage, ev.Fingerprint.EventsKey(), in.Fingerprint.EventsKey(), fp.EventsKey())
		}
		if !slices.EqualFunc(ev.Events, in.Events, sameEvent) || !slices.EqualFunc(ev.Notable, in.Notable, sameEvent) ||
			ev.EventsComplete != in.EventsComplete || ev.NotableComplete != in.NotableComplete || ev.Snapshot != nil || len(ev.FrameworkProducts) != 0 {
			t.Fatalf("%s: LoadEvents read %d+%d rows (%v %v), Load %d+%d (%v %v)", stage, len(ev.Events), len(ev.Notable),
				ev.EventsComplete, ev.NotableComplete, len(in.Events), len(in.Notable), in.EventsComplete, in.NotableComplete)
		}
	}
	same("empty")
	db.Exec(t, "INSERT INTO test_events(at,kind,signal,severity) VALUES ('2026-11-02T15:00:00Z','pulse_down',-1,'major'),"+
		"('2026-11-02T14:00:00Z','latency_rise',-1,'minor')")
	db.Exec(t, "INSERT INTO test_events(at,kind,signal,severity) SELECT '2026-11-02T16:00:30Z','estimate_hour_released',0,'info'"+
		" FROM generate_series(1,$1::int)", store.EventWindow+1)
	same("window cut")
	if ev, err := store.LoadEvents(ctx, pool); err != nil || ev.EventsComplete || len(ev.Notable) != 2 || ev.Fingerprint.EventsKey() == "" {
		t.Fatalf("cut window %v %d notable %q", err, len(ev.Notable), ev.Fingerprint.EventsKey())
	}
	done, cancel := context.WithCancel(ctx)
	cancel()
	if _, err := store.LoadEvents(done, pool); err == nil {
		t.Fatal("LoadEvents read with a cancelled context")
	}
}

func sameEvent(a, b store.Event) bool {
	return a.ID == b.ID && a.At.Equal(b.At) && a.Kind == b.Kind && string(a.Data) == string(b.Data)
}
