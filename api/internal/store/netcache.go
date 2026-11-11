package store

import (
	"cmp"
	"container/list"
	"slices"
	"sync"
	"unsafe"
)

// DefaultRowCacheBytes bounds the network-list rows a NetLists keeps once
// read (API-MEM-1), unless Lists.RowCacheBytes sets it (siteapi serve
// -row-cache): the rows readers asked for last, by list and dimension. The
// two real days on proxy_reviews have 265 dimensions, 311 MB (ROW-CACHE-1):
// 256 MiB left 177 of them cold, and a cold list's first q= search missed its
// 50 ms budget; 512 MiB holds them all, the siteapi's peak at 2.18 GB.
const DefaultRowCacheBytes = 512 << 20

// netRowBytes is the size of one compact row.
const netRowBytes = int(unsafe.Sizeof(netRow{}))

// rowKey is one dimension (its index in netCodes) of one list.
type rowKey struct {
	id int64
	d  int
}

// rowCache is a least-recently-used cache of read rows within limit bytes
// (DefaultRowCacheBytes); a dimension larger than that is read again each time it is
// asked for.
type rowCache struct {
	limit int
	mu    sync.Mutex
	order *list.List // of *rowEntry, the most recent first
	at    map[rowKey]*list.Element
	bytes int
}

type rowEntry struct {
	key  rowKey
	rows *NetRows
}

// size is the bytes rows takes in the cache (its names are shared).
func size(rows *NetRows) int { return len(rows.rows) * netRowBytes }

// get is the cached rows of k, nil when not cached.
func (c *rowCache) get(k rowKey) *NetRows {
	c.mu.Lock()
	defer c.mu.Unlock()
	e := c.at[k]
	if e == nil {
		return nil
	}
	c.order.MoveToFront(e)
	return e.Value.(*rowEntry).rows
}

// add caches rows as k, dropping the least recently used rows past the bound.
func (c *rowCache) add(k rowKey, rows *NetRows) {
	if size(rows) > c.limit {
		return
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.at == nil {
		c.order, c.at = list.New(), map[rowKey]*list.Element{}
	}
	if c.at[k] != nil {
		return
	}
	c.at[k] = c.order.PushFront(&rowEntry{k, rows})
	c.bytes += size(rows)
	c.evict()
}

// warm caches as many of entries as limit holds, in warmOrder, as the most
// recently used, the first in front; it drops the least recently used other
// rows past the bound. It returns the entries it left out (cold), the
// largest first.
func (c *rowCache) warm(entries []rowEntry) (cold []rowEntry) {
	var kept []rowEntry
	total := 0
	for _, e := range warmOrder(entries) {
		if total+size(e.rows) > c.limit {
			cold = append(cold, e)
			continue
		}
		kept = append(kept, e)
		total += size(e.rows)
	}
	slices.SortStableFunc(cold, func(a, b rowEntry) int { return -cmp.Compare(size(a.rows), size(b.rows)) })
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.at == nil {
		c.order, c.at = list.New(), map[rowKey]*list.Element{}
	}
	for _, e := range slices.Backward(kept) {
		if el := c.at[e.key]; el != nil {
			c.order.MoveToFront(el)
			continue
		}
		c.at[e.key] = c.order.PushFront(&rowEntry{e.key, e.rows})
		c.bytes += size(e.rows)
	}
	c.evict()
	return cold
}

// warmOrder is entries with each list's smallest dimension first, the
// smallest lists first, then the other dimensions, the smallest first
// (ROW-CACHE-1): a list is left wholly cold only when its smallest dimension
// does not fit beside the smaller lists' smallest. A cold dimension's first
// search reads it from the database: over its 50 ms budget however small it
// is, as a read's round trips cost more than its rows.
func warmOrder(entries []rowEntry) []rowEntry {
	slices.SortFunc(entries, func(a, b rowEntry) int {
		return cmp.Or(cmp.Compare(size(a.rows), size(b.rows)), cmp.Compare(a.key.id, b.key.id), cmp.Compare(a.key.d, b.key.d))
	})
	first, rest := make([]rowEntry, 0, len(entries)), []rowEntry{}
	seen := map[int64]bool{}
	for _, e := range entries {
		if seen[e.key.id] {
			rest = append(rest, e)
			continue
		}
		seen[e.key.id] = true
		first = append(first, e)
	}
	return append(first, rest...)
}

// evict drops the least recently used rows past the bound.
func (c *rowCache) evict() {
	for c.bytes > c.limit {
		e := c.order.Remove(c.order.Back()).(*rowEntry)
		delete(c.at, e.key)
		c.bytes -= size(e.rows)
	}
}
