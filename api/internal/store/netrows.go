package store

import (
	"cmp"
	"context"
	"fmt"
	"slices"
	"sync"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"golang.org/x/sync/errgroup"
)

// A rebuild reads every row of its network lists: the load each list's
// country and ASN rows (listWindow), the build every dimension, merged per
// network, country and window. Read one at a time through the row cache, that
// was 443 reads of 6.35M rows on the 7-day staging data (the cache is smaller
// than the lists, so the build read a dimension again when it came back to
// it), each as fast as one connection carries rows: 69 s of a 95 s rebuild
// (SITEAPI-REBUILD-1). So the load reads each dimension of its lists once,
// over NetReadConns connections at a time, largest first, into a NetRowSet;
// the load and the build take their rows from it. Pages and searches read
// through the row cache, which the rebuild fills from the set before it
// serves the new build (Warm): a search's first request reads no row from
// the database (COVERAGE-SEARCH-1).

// NetReadConns is how many connections a rebuild reads its network lists
// over at a time; OpenReadOnlyFor's pool has more, for the load's
// transaction and requests.
const NetReadConns = 6

// NetReadParallelRows is the fewest rows a load's lists hold for it to read
// them over connections of their own: fewer (about half a second's reading)
// are read in the load's transaction, opening no connection.
const NetReadParallelRows = 50_000

// NetRowSet is every dimension's rows of one load's network lists, read by
// src.
type NetRowSet struct {
	mu   sync.Mutex
	rows map[rowKey]*NetRows
	src  *NetLists
}

// Warmed is what Warm put in the row cache, and what it left out because the
// cache's limit (Limit bytes) holds less: dimensions, rows and bytes, and the
// dimensions left out, the largest first.
type Warmed struct {
	Dimensions, Rows, Bytes, Limit int
	ColdDimensions, ColdRows       int
	Cold                           []ColdList
}

// ColdList is one dimension of a network list that Warm left out: its first
// search reads its rows from the database.
type ColdList struct {
	List      int64  `json:"list"`
	Dimension string `json:"dimension"`
	Rows      int    `json:"rows"`
}

// Warm puts every dimension the set holds in its lists' row cache, each
// list's smallest first (rowCache.warm) while the cache's limit holds them
// (every dimension of a network list is searched by a q=: its place lists and
// its City x ASN coverage), so that requests read none from the database. The
// rows are those the set holds: the cache adds no copy. A nil set warms
// nothing.
func (set *NetRowSet) Warm() Warmed {
	if set == nil || set.src == nil {
		return Warmed{}
	}
	set.mu.Lock()
	entries := make([]rowEntry, 0, len(set.rows))
	for k, r := range set.rows {
		entries = append(entries, rowEntry{k, r})
	}
	set.mu.Unlock()
	cache := &set.src.cache
	cold := cache.warm(entries)
	w := Warmed{Dimensions: len(entries) - len(cold), Limit: cache.limit, ColdDimensions: len(cold)}
	for _, e := range cold {
		w.ColdRows += e.rows.Len()
		w.Cold = append(w.Cold, ColdList{List: e.key.id, Dimension: netEntries[e.key.d], Rows: e.rows.Len()})
	}
	for _, e := range entries {
		w.Rows += e.rows.Len()
	}
	w.Rows -= w.ColdRows
	w.Bytes = w.Rows * netRowBytes
	return w
}

type netRowsKey struct{}

// WithNetRows is ctx whose reads (RowsOf) take the rows set holds from it.
func WithNetRows(ctx context.Context, set *NetRowSet) context.Context {
	if set == nil {
		return ctx
	}
	return context.WithValue(ctx, netRowsKey{}, set)
}

// RowsOf is a dimension's rows of l: from ctx's NetRowSet when it holds
// them, else l.Rows.
func RowsOf(ctx context.Context, l *NetList, dimension string) (*NetRows, error) {
	return rowsFrom(ctx, nil, l, dimension)
}

// rowsFrom is RowsOf, read in tx when neither ctx's set nor the cache holds
// them (nil: a transaction of their own).
func rowsFrom(ctx context.Context, tx pgx.Tx, l *NetList, dimension string) (*NetRows, error) {
	if set, _ := ctx.Value(netRowsKey{}).(*NetRowSet); set != nil && l != nil {
		set.mu.Lock()
		r := set.rows[rowKey{l.id, dimIndex(dimension)}]
		set.mu.Unlock()
		if r != nil {
			return r, nil
		}
	}
	return l.rowsIn(ctx, tx, dimension)
}

// readAll reads every dimension with rows of the lists that read from the
// database: over up to NetReadConns read-only transactions of their own when
// n reads through a pool and they hold at least minRows rows, else in tx
// (the load's) one after another: a pgx.Conn has one transaction at a time.
func (n *NetLists) readAll(ctx context.Context, tx pgx.Tx, lists map[int64]*NetList, minRows int) (*NetRowSet, error) {
	var keys []rowKey
	total := 0
	for id, l := range lists {
		for d := range netCodes {
			if l != nil && l.src == n && l.count[d] > 0 {
				keys = append(keys, rowKey{id, d})
				total += l.count[d]
			}
		}
	}
	slices.SortFunc(keys, func(a, b rowKey) int {
		return cmp.Or(-cmp.Compare(lists[a.id].count[a.d], lists[b.id].count[b.d]), cmp.Compare(a.id, b.id), cmp.Compare(a.d, b.d))
	})
	set := &NetRowSet{rows: make(map[rowKey]*NetRows, len(keys)), src: n}
	read := func(tx pgx.Tx, k rowKey) error {
		r, err := n.readOne(ctx, tx, lists[k.id], k.d)
		if err != nil {
			return err
		}
		set.mu.Lock()
		set.rows[k] = r
		set.mu.Unlock()
		return nil
	}
	if _, ok := n.db.(*pgxpool.Pool); !ok || total < minRows {
		for _, k := range keys {
			if err := read(tx, k); err != nil {
				return nil, err
			}
		}
		return set, nil
	}
	return set, n.readParallel(ctx, keys, read)
}

// readParallel calls read for every key, in order, from up to NetReadConns
// read-only transactions; the first error stops the others.
func (n *NetLists) readParallel(ctx context.Context, keys []rowKey, read func(pgx.Tx, rowKey) error) error {
	next := make(chan rowKey)
	g, ctx := errgroup.WithContext(ctx)
	for range min(NetReadConns, len(keys)) {
		g.Go(func() error {
			tx, err := n.db.BeginTx(ctx, pgx.TxOptions{AccessMode: pgx.ReadOnly})
			if err != nil {
				return fmt.Errorf("network lists: %w", err)
			}
			defer tx.Rollback(ctx) //nolint:errcheck // read-only
			for k := range next {
				if err := read(tx, k); err != nil {
					return err
				}
			}
			return nil
		})
	}
	g.Go(func() error {
		defer close(next)
		for _, k := range keys {
			select {
			case next <- k:
			case <-ctx.Done():
				return nil
			}
		}
		return nil
	})
	return g.Wait()
}

// readOne reads dimension d of l in tx: its rows outside the lock, so that
// several transactions read at once, then their names, as rows does.
func (n *NetLists) readOne(ctx context.Context, tx pgx.Tx, l *NetList, d int) (*NetRows, error) {
	scanned := make([]scannedRow, 0, l.count[d])
	err := scanList(ctx, tx, true, 0, []int64{l.id}, netCodes[d:d+1], func(s *scannedRow) error {
		scanned = append(scanned, *s)
		return nil
	})
	if err != nil {
		return nil, fmt.Errorf("network list %d: %w", l.id, err)
	}
	n.reading.Lock()
	defer n.reading.Unlock()
	n.Reads++
	r, err := n.adopt(ctx, tx, l, d, scanned)
	if err != nil {
		n.reset()
		return nil, fmt.Errorf("network list %d: %w", l.id, err)
	}
	n.cache.add(rowKey{l.id, d}, r)
	return r, nil
}

// adopt is the scanned rows of dimension d of l as compact rows, with the
// names they created read in tx (read's steps after its scan). n.reading is
// held.
func (n *NetLists) adopt(ctx context.Context, tx pgx.Tx, l *NetList, d int, scanned []scannedRow) (*NetRows, error) {
	n.missing = nil
	from := [2]int{len(n.names.places), len(n.names.networks)}
	rows := make([]netRow, len(scanned))
	for i := range scanned {
		var err error
		if rows[i], err = n.compact(&scanned[i]); err != nil {
			return nil, err
		}
	}
	if err := n.readLabels(ctx, tx); err != nil {
		return nil, err
	}
	if len(rows) != l.count[d] {
		return nil, fmt.Errorf("%d %s rows, %d when it was loaded: the list was deleted", len(rows), netCodes[d:d+1], l.count[d])
	}
	n.fillTexts(from)
	return &NetRows{rows: rows, names: n.names}, nil
}
