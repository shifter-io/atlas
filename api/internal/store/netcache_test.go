package store

import (
	"math/rand/v2"
	"testing"
)

// cachedKeys is c's keys, the most recently used first.
func cachedKeys(c *rowCache) []rowKey {
	var out []rowKey
	for e := c.order.Front(); e != nil; e = e.Next() {
		out = append(out, e.Value.(*rowEntry).key)
	}
	return out
}

// A warm step caches the largest rows first while the limit holds them, in
// front of the rows cached before, which go first when the limit is reached;
// rows already cached stay as they were; it returns the rows it left out
// (COVERAGE-SEARCH-1).
func TestRowCacheWarmsTheLargestFirstWithinItsLimit(t *testing.T) {
	t.Parallel()
	rows := func(n int) *NetRows { return &NetRows{rows: make([]netRow, n)} }
	c := rowCache{limit: 10 * netRowBytes}
	old, held := rows(3), rows(2)
	c.add(rowKey{1, 0}, old)
	c.add(rowKey{2, 0}, held)
	big, mid, small, huge, left := rows(4), rows(3), rows(1), rows(11), rows(2)
	cold := c.warm([]rowEntry{{rowKey{5, 0}, small}, {rowKey{2, 0}, rows(2)}, {rowKey{3, 0}, big}, {rowKey{9, 1}, huge},
		{rowKey{4, 0}, mid}, {rowKey{6, 0}, left}})
	// 4 + 3 + 2 rows, then list 6's 2 do not fit (11) but list 5's 1 does.
	if len(cold) != 2 || cold[0].key != (rowKey{9, 1}) || cold[1].key != (rowKey{6, 0}) {
		t.Fatalf("left out %+v, want lists 9 and 6", cold)
	}
	want := []rowKey{{3, 0}, {4, 0}, {2, 0}, {5, 0}}
	if got := cachedKeys(&c); len(got) != len(want) || c.bytes != 10*netRowBytes {
		t.Fatalf("cached %v (%d bytes), want %v: list 1, used least recently, goes", got, c.bytes, want)
	}
	for i, k := range cachedKeys(&c) {
		if k != want[i] {
			t.Errorf("cached %d: list %d, want %d", i, k.id, want[i].id)
		}
	}
	if c.get(rowKey{2, 0}) != held || c.get(rowKey{3, 0}) != big || c.get(rowKey{1, 0}) != nil {
		t.Error("the warm step replaced cached rows or kept list 1")
	}
}

// Whatever the cache held and the warm step is given, the cache holds at most
// its limit, and every row left out would not have fit.
func TestRowCacheWarmStaysWithinItsLimit(t *testing.T) {
	t.Parallel()
	r := rand.New(rand.NewPCG(7, 97))
	for round := range 200 {
		c := rowCache{limit: (1 + r.IntN(40)) * netRowBytes}
		for i := range r.IntN(8) {
			c.add(rowKey{int64(100 + i), 0}, &NetRows{rows: make([]netRow, 1+r.IntN(12))})
		}
		var entries []rowEntry
		for i := range r.IntN(12) {
			entries = append(entries, rowEntry{rowKey{int64(i), r.IntN(5)}, &NetRows{rows: make([]netRow, 1+r.IntN(20))}})
		}
		cold := c.warm(entries)
		warmed := 0
		for _, e := range entries {
			if c.at[e.key] != nil {
				warmed += size(e.rows)
			}
		}
		if c.bytes > c.limit || warmed > c.limit {
			t.Fatalf("round %d: %d bytes held, %d warmed, limit %d", round, c.bytes, warmed, c.limit)
		}
		for _, e := range cold {
			if c.at[e.key] != nil || warmed+size(e.rows) <= c.limit {
				t.Errorf("round %d: list %d left out (%d bytes) beside %d warmed bytes, limit %d", round, e.key.id, size(e.rows), warmed, c.limit)
			}
		}
	}
}
