package server

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"testing"
	"time"

	"github.com/Zeus-Development/proxy-index/api/internal/build"
	"github.com/Zeus-Development/proxy-index/api/internal/config"
	"github.com/Zeus-Development/proxy-index/api/internal/store"
	"github.com/Zeus-Development/proxy-index/api/internal/testdb"
)

var beforeStart = time.Date(2026, 11, 10, 8, 0, 0, 0, time.UTC)

// insertBeforeHistory writes six pulse slots of before-a (residential) and
// before-b (datacenter), as build's history test fixture: per slot a census
// of both at one time, each target's pulse in US and DE at one time, a
// two-target down of before-a in DE whose generic_https row has the group's
// largest id but stands below its generic_http row, a success_drop of
// before-b between them, a two-target recovered at one time and two no_node
// rows at that time too.
func insertBeforeHistory(t *testing.T, db *testdb.DB) {
	db.Exec(t, "INSERT INTO providers VALUES ('before-a'),('before-b')")
	db.Exec(t, "INSERT INTO products(provider,name) VALUES ('before-a','residential'),('before-b','datacenter')")
	db.Exec(t, `INSERT INTO test_batches(kind,country_code,region_id,target_id,scheduled_for,start_at)
 SELECT 'pulse',c,1,t,$1::timestamptz + s * interval '30 minutes',$1::timestamptz
 FROM unnest(ARRAY['DE','US']) c, unnest(ARRAY['generic_http','generic_https']) t, generate_series(0, 5) s`, beforeStart)
	add := func(m int, kind, provider, country, target string, slot int) {
		var tg, sl any
		if target != "" {
			tg = target
		}
		if slot >= 0 {
			sl = beforeStart.Add(time.Duration(slot*30) * time.Minute)
		}
		db.Exec(t, `INSERT INTO test_events(at,kind,batch_id,product_id,country_code,target_id,region_id,signal,severity)
 SELECT $1::timestamptz, $2, (SELECT batch_id FROM test_batches WHERE kind='pulse' AND country_code=$4 AND target_id=$5 AND scheduled_for=$6),
 product_id, $4, $5, CASE WHEN $5::text IS NULL THEN NULL ELSE 1 END, 0, 'info' FROM products WHERE provider=$3`,
			beforeStart.Add(time.Duration(m)*time.Minute), kind, provider, country, tg, sl)
	}
	for s := range 6 {
		m := s * 30
		add(m+1, "census_finished", "before-a", "US", "", -1)
		add(m+1, "census_finished", "before-b", "DE", "", -1)
		for _, c := range []string{"US", "DE"} {
			add(m+6, "pulse_finished", "before-a", c, "generic_http", s)
			add(m+6, "pulse_finished", "before-a", c, "generic_https", s)
		}
		add(m+9, "down", "before-a", "DE", "generic_http", s)
		add(m+8, "success_drop", "before-b", "US", "generic_http", s)
		add(m+7, "down", "before-a", "DE", "generic_https", s)
		add(m+12, "recovered", "before-a", "US", "generic_https", s)
		add(m+12, "recovered", "before-a", "US", "generic_http", s)
		add(m+12, "no_node", "before-b", "DE", "", -1)
		add(m+12, "no_node", "before-b", "DE", "", -1)
	}
}

// EVENTS-BEFORE-1 against the database: the fallback's before read
// (store.QueryEvents, its Limit+1 rows below the anchor's place and the
// group rows above it) pages back through the history with each filter as
// the complete window does, every row exactly once and whole, more true on
// every page but the last; a before that names no event is
// store.ErrUnknownCursor, 400 on the route.
func TestBeforePagesFromTheDatabase(t *testing.T) {
	t.Parallel()
	db := testdb.New(t)
	ctx := context.Background()
	snap, err := os.ReadFile(config.FindUp(filepath.Join("fixtures", "measured", "snapshot.json")))
	if err != nil {
		t.Fatal(err)
	}
	db.PublishV4(t, snap)
	insertBeforeHistory(t, db)
	pool, err := store.OpenReadOnly(ctx, db.DSN)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	s := newTestServer(t, DBSource{Pool: pool})
	w := &s.Current().Result.Events
	since := beforeStart.Add(35 * time.Minute)
	filters := map[string]store.EventFilter{
		"none": {}, "network": {Provider: "before-a"}, "product": {Product: "before-b/datacenter"}, "kind": {Kinds: []string{"down", "pulse_finished"}},
		"category": {Category: "incident"}, "country": {Country: "DE"}, "target": {Target: "generic_https"}, "q": {Words: []string{"de"}},
		"proxyKind": {ProxyKind: "residential"}, "since": {Since: &since}, "since and category": {Since: &since, Category: "pulse"},
	}
	for name, f := range filters {
		for _, limit := range []int{1, 3} {
			t.Run(fmt.Sprintf("%s/%d", name, limit), func(t *testing.T) {
				all := f
				all.Limit = MaxEvents
				full, ok := w.Answer(all)
				if !ok || len(full.Events) < 3*limit+2 {
					t.Fatalf("the window's answer: %v, %d rows", ok, len(full.Events))
				}
				got, page := []build.EventRow{}, f
				page.Limit, page.Before = limit, &full.Events[0].ID
				for reads := 0; ; reads++ {
					if reads > len(full.Events) {
						t.Fatal("before does not move")
					}
					rows, err := store.QueryEvents(ctx, pool, page)
					if err != nil {
						t.Fatal(err)
					}
					a := w.FromRows(page, rows)
					got = append(got, a.Events...)
					if !a.More {
						break
					}
					page.Before = &a.Events[len(a.Events)-1].ID
				}
				same := func(a, b build.EventRow) bool { return a.ID == b.ID && slices.Equal(a.Targets, b.Targets) }
				if !slices.EqualFunc(got, full.Events[1:], same) {
					t.Fatalf("pages %v, want %v", eventIDsOf(got), eventIDsOf(full.Events[1:]))
				}
			})
		}
	}
	unknown := int64(1 << 40)
	if _, err := store.QueryEvents(ctx, pool, store.EventFilter{Before: &unknown, Limit: 5}); !errors.Is(err, store.ErrUnknownCursor) {
		t.Fatalf("an unknown before: %v", err)
	}
	if r := get(s.Handler(), fmt.Sprintf("/v1/events?before=%d", unknown)); r.Code != 400 {
		t.Fatalf("an unknown before on the route: %d %s", r.Code, r.Body.String())
	}
	if b := readTape(t, s, "/v1/events?limit=5"); len(b.Events) != 5 || !b.More {
		t.Fatalf("the first page: %v more %v", b.ids(), b.More)
	}
}
