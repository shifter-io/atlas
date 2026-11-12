package store_test

import (
	"context"
	"maps"
	"reflect"
	"slices"
	"strings"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/store"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

var netDimensions = []string{"country", "region", "city", "asn", "city_asn"}

// sameRows fails t unless every dimension of every list reads from ctx's set
// the rows that a fresh NetLists over pool reads one by one.
func sameRows(t *testing.T, ctx context.Context, pool *pgxpool.Pool, lists map[int64]*store.NetList) {
	t.Helper()
	var serial map[int64]*store.NetList
	within(t, pool, func(tx pgx.Tx) { serial = loadNet(t, store.NewNetLists(pool), tx, slices.Collect(maps.Keys(lists))...) })
	for id, l := range lists {
		for _, name := range netDimensions {
			got, err := store.RowsOf(ctx, l, name)
			if err != nil {
				t.Fatal(err)
			}
			want := dim(t, serial[id], name)
			if got.Len() != want.Len() || got.Len() != l.Len(name) || got.Len() == 0 {
				t.Fatalf("list %d %s: %d rows, want %d", id, name, got.Len(), want.Len())
			}
			for i := range want.Len() {
				if g, w := got.Row(i), want.Row(i); !reflect.DeepEqual(g, w) {
					t.Errorf("list %d %s row %d: %+v, want %+v", id, name, i, g, w)
				}
			}
		}
	}
}

// A load's lists are read once each dimension, over transactions of their
// own when the lists read through a pool, and give the rows the row cache
// gives; reads from the set read nothing more.
func TestReadAllReadsEveryDimensionOnce(t *testing.T) {
	t.Parallel()
	_, pool := networkLists(t)
	n := store.NewNetLists(pool)
	ctx := context.Background()
	within(t, pool, func(tx pgx.Tx) {
		lists := loadNet(t, n, tx, 1, 2, 3)
		set, err := n.ReadAll(ctx, tx, lists, 0)
		if err != nil || n.Reads != 15 {
			t.Fatalf("%d reads (%v), want 15: 3 lists x 5 dimensions", n.Reads, err)
		}
		in := store.WithNetRows(ctx, set)
		for _, name := range netDimensions {
			if _, err := store.RowsOf(in, lists[2], name); err != nil || n.Reads != 15 {
				t.Fatalf("%s from the set: %d reads (%v)", name, n.Reads, err)
			}
		}
		sameRows(t, in, pool, lists)
	})
}

// Through a single connection (a pgx.Conn has one transaction at a time),
// the lists are read one after another in the load's transaction.
func TestReadAllThroughAConnection(t *testing.T) {
	t.Parallel()
	db, pool := networkLists(t)
	ctx := context.Background()
	c := db.Conn(t)
	defer c.Close(ctx)
	tx, err := c.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.RepeatableRead, AccessMode: pgx.ReadOnly})
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback(ctx) //nolint:errcheck
	n := store.NewNetLists(c)
	lists := loadNet(t, n, tx, 1, 2)
	set, err := n.ReadAll(ctx, tx, lists, 0)
	if err != nil || n.Reads != 10 {
		t.Fatalf("%d reads (%v), want 10", n.Reads, err)
	}
	sameRows(t, store.WithNetRows(ctx, set), pool, lists)
	// Through a pool, lists of fewer rows than asked are read the same way:
	// in the load's transaction, which sees no later delete.
	byPool := store.NewNetLists(pool)
	within(t, pool, func(tx pgx.Tx) {
		few := loadNet(t, byPool, tx, 3)
		db.Exec(t, `DELETE FROM calculated_list_rows WHERE list_id = 3`)
		set, err := byPool.ReadAll(ctx, tx, few, store.NetReadParallelRows)
		if r, _ := store.RowsOf(store.WithNetRows(ctx, set), few[3], "city"); err != nil || r == nil || r.Len() != 2 {
			t.Errorf("list 3 in the load's transaction: %v (%v)", r, err)
		}
	})
	if got, err := store.RowsOf(ctx, nil, "city"); err != nil || got.Len() != 0 {
		t.Errorf("no list: %v rows (%v)", got, err)
	}
}

// A list deleted after its counts were read fails the read, whichever
// transaction reads it, and a failed read leaves the names consistent: the
// next read works.
func TestReadAllOfADeletedList(t *testing.T) {
	t.Parallel()
	db, pool := networkLists(t)
	n := store.NewNetLists(pool)
	ctx := context.Background()
	var lists map[int64]*store.NetList
	within(t, pool, func(tx pgx.Tx) { lists = loadNet(t, n, tx, 1, 2, 3) })
	db.Exec(t, `DELETE FROM calculated_list_rows WHERE list_id = 2 AND dimension = 'y'`)
	within(t, pool, func(tx pgx.Tx) {
		if _, err := n.ReadAll(ctx, tx, lists, 0); err == nil || !strings.Contains(err.Error(), "network list 2: 0 y rows, 2 when it was loaded") {
			t.Errorf("a deleted list: %v", err)
		}
	})
	delete(lists, 2)
	within(t, pool, func(tx pgx.Tx) {
		set, err := n.ReadAll(ctx, tx, lists, 0)
		if err != nil {
			t.Fatal(err)
		}
		sameRows(t, store.WithNetRows(ctx, set), pool, lists)
	})
	within(t, pool, func(tx pgx.Tx) {
		tx.Rollback(ctx) //nolint:errcheck
		c, cancel := context.WithCancel(ctx)
		cancel()
		if _, err := n.ReadAll(c, tx, lists, 0); err == nil {
			t.Error("a read with a cancelled context succeeded")
		}
	})
}

// A set's Warm puts its dimensions in the row cache, each list's smallest
// first, then the others, the smallest first, while the limit holds them, so
// that requests read those from the cache, even once the rows are deleted,
// and the others from the database (COVERAGE-SEARCH-1, ROW-CACHE-1): under a
// 9-row limit every list keeps its region row, and list 1 its 2-row
// dimensions too. A nil set warms nothing.
func TestWarmFillsTheRowCache(t *testing.T) {
	t.Parallel()
	db, pool := networkLists(t)
	ctx := context.Background()
	load := func(limit int) (*store.NetLists, map[int64]*store.NetList, *store.NetRowSet) {
		n := store.NewNetLists(pool)
		n.SetRowCache(limit)
		var lists map[int64]*store.NetList
		var set *store.NetRowSet
		within(t, pool, func(tx pgx.Tx) {
			lists = loadNet(t, n, tx, 1, 2, 3)
			var err error
			if set, err = n.ReadAll(ctx, tx, lists, 0); err != nil {
				t.Fatal(err)
			}
		})
		return n, lists, set
	}
	cold := func(list int64, dimension string, rows int) store.ColdList {
		return store.ColdList{List: list, Dimension: dimension, Rows: rows}
	}
	// Each list: 3 ASN rows, 2 country, city and City x ASN rows, 1 region row.
	all, allLists, allSet := load(store.DefaultRowCacheBytes)
	few, fewLists, fewSet := load(9 * 28)
	want := map[*store.NetRowSet]store.Warmed{
		allSet: {Dimensions: 15, Rows: 30, Bytes: 30 * 28, Limit: store.DefaultRowCacheBytes},
		fewSet: {Dimensions: 6, Rows: 9, Bytes: 9 * 28, Limit: 9 * 28, ColdDimensions: 9, ColdRows: 21, Cold: []store.ColdList{
			cold(1, "asn", 3), cold(2, "asn", 3), cold(3, "asn", 3), cold(2, "country", 2), cold(2, "city", 2), cold(2, "city_asn", 2),
			cold(3, "country", 2), cold(3, "city", 2), cold(3, "city_asn", 2)}},
	}
	for set, w := range want {
		if got := set.Warm(); !reflect.DeepEqual(got, w) {
			t.Errorf("warmed %+v, want %+v", got, w)
		}
	}
	db.Exec(t, `DELETE FROM calculated_list_rows`)
	for id := range allLists {
		for _, name := range netDimensions {
			got, err := allLists[id].Rows(ctx, name)
			if want, _ := store.RowsOf(store.WithNetRows(ctx, allSet), allLists[id], name); err != nil || got != want {
				t.Errorf("list %d %s after the warm: %v, want the set's rows (%v)", id, name, got, err)
			}
			_, err = fewLists[id].Rows(ctx, name)
			if warm := name == "region" || id == 1 && name != "asn"; (err == nil) != warm {
				t.Errorf("list %d %s under a 9-row limit: read %v, want from the cache: %v", id, name, err, warm)
			}
		}
	}
	if all.Reads != 15 || few.Reads != 15+9 {
		t.Errorf("%d and %d reads, want 15 and 24: the warmed dimensions read none", all.Reads, few.Reads)
	}
	if w := (*store.NetRowSet)(nil).Warm(); !reflect.DeepEqual(w, store.Warmed{}) {
		t.Errorf("a nil set warmed %+v", w)
	}
}
