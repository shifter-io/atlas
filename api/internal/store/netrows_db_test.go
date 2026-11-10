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

// A load leaves the row cache to the serving build; its set's Warm puts
// every dimension in the cache, so that requests read none from the database,
// even once the rows are deleted (COVERAGE-SEARCH-1). A nil set warms nothing.
func TestWarmFillsTheRowCache(t *testing.T) {
	t.Parallel()
	db, pool := networkLists(t)
	n := store.NewNetLists(pool)
	ctx := context.Background()
	var lists map[int64]*store.NetList
	var set *store.NetRowSet
	within(t, pool, func(tx pgx.Tx) {
		lists = loadNet(t, n, tx, 1, 2, 3)
		var err error
		if set, err = n.ReadAll(ctx, tx, lists, 0); err != nil {
			t.Fatal(err)
		}
	})
	before, err := lists[1].Rows(ctx, "city")
	if err != nil || n.Reads != 16 {
		t.Fatalf("a read before the warm: %d reads (%v), want 16: the load cached none", n.Reads, err)
	}
	w := set.Warm()
	want := store.Warmed{Dimensions: 15, Rows: 30, Bytes: 30 * 24, Limit: store.DefaultRowCacheBytes}
	if !reflect.DeepEqual(w, want) {
		t.Errorf("warmed %+v, want %+v", w, want)
	}
	db.Exec(t, `DELETE FROM calculated_list_rows`)
	for id, l := range lists {
		for _, name := range netDimensions {
			got, err := l.Rows(ctx, name)
			want, _ := store.RowsOf(store.WithNetRows(ctx, set), l, name)
			if id == 1 && name == "city" {
				want = before // cached before the warm, which replaces no cached rows
			}
			if err != nil || got != want {
				t.Errorf("list %d %s after the warm: %v, want the set's rows (%v)", id, name, got, err)
			}
		}
	}
	if n.Reads != 16 {
		t.Errorf("%d reads after the warm, want 16", n.Reads)
	}
	if w := (*store.NetRowSet)(nil).Warm(); !reflect.DeepEqual(w, store.Warmed{}) {
		t.Errorf("a nil set warmed %+v", w)
	}
}
