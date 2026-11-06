package store

import (
	"container/list"
	"sync"
	"unsafe"
)

// rowCacheBytes bounds the network-list rows a NetLists keeps once read
// (API-MEM-1): the rows readers asked for last, by list and dimension.
const rowCacheBytes = 64 << 20

// netRowBytes is the size of one compact row.
const netRowBytes = int(unsafe.Sizeof(netRow{}))

// rowKey is one dimension (its index in netCodes) of one list.
type rowKey struct {
	id int64
	d  int
}

// rowCache is a least-recently-used cache of read rows within limit bytes
// (rowCacheBytes); a dimension larger than that is read again each time it is
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
	for c.bytes > c.limit {
		e := c.order.Remove(c.order.Back()).(*rowEntry)
		delete(c.at, e.key)
		c.bytes -= size(e.rows)
	}
}
