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

// A warm step caches each list's smallest rows first, the smallest lists
// first, then the other rows, the smallest first, while the limit holds them
// (ROW-CACHE-1): no list is left wholly cold while its smallest rows fit. They
// go in front of the rows cached before, which go first when the limit is
// reached; rows already cached stay as they were; it returns the rows it left
// out, the largest first.
func TestRowCacheWarmsEveryListsSmallestPartFirst(t *testing.T) {
	t.Parallel()
	rows := func(n int) *NetRows { return &NetRows{rows: make([]netRow, n)} }
	c := rowCache{limit: 10 * netRowBytes}
	old, held := rows(3), rows(2)
	c.add(rowKey{1, 0}, old)
	c.add(rowKey{2, 0}, held)
	small, big, mid, huge, left := rows(1), rows(4), rows(3), rows(11), rows(2)
	cold := c.warm([]rowEntry{{rowKey{5, 0}, small}, {rowKey{5, 1}, rows(1)}, {rowKey{5, 2}, rows(1)}, {rowKey{5, 3}, rows(1)},
		{rowKey{2, 0}, rows(2)}, {rowKey{3, 0}, big}, {rowKey{9, 1}, huge}, {rowKey{4, 0}, mid}, {rowKey{6, 0}, left}})
	// Each list's smallest: 5's 1, 2's 2, 6's 2, 4's 3 (8 rows), then 5's other
	// 1-row dimensions while they fit: 2 of 3; 3's 4 and 9's 11 do not.
	wantCold := []rowKey{{9, 1}, {3, 0}, {5, 3}}
	if len(cold) != len(wantCold) {
		t.Fatalf("left out %+v, want %v", cold, wantCold)
	}
	for i, e := range cold {
		if e.key != wantCold[i] {
			t.Errorf("left out %d: %v, want %v", i, e.key, wantCold[i])
		}
	}
	want := []rowKey{{5, 0}, {2, 0}, {6, 0}, {4, 0}, {5, 1}, {5, 2}}
	if got := cachedKeys(&c); len(got) != len(want) || c.bytes != 10*netRowBytes {
		t.Fatalf("cached %v (%d bytes), want %v: list 1, used least recently, goes", got, c.bytes, want)
	}
	for i, k := range cachedKeys(&c) {
		if k != want[i] {
			t.Errorf("cached %d: %v, want %v", i, k, want[i])
		}
	}
	if c.get(rowKey{2, 0}) != held || c.get(rowKey{5, 0}) != small || c.get(rowKey{4, 0}) != mid || c.get(rowKey{1, 0}) != nil {
		t.Error("the warm step replaced cached rows or kept list 1")
	}
}

// Whatever the cache held and the warm step is given, the cache holds at most
// its limit, every row left out would not have fit, and when every list's
// smallest rows fit, every list has rows warm.
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
			entries = append(entries, rowEntry{rowKey{int64(i % 4), i / 4}, &NetRows{rows: make([]netRow, 1+r.IntN(20))}})
		}
		smallest := map[int64]int{}
		for _, e := range entries {
			if n, ok := smallest[e.key.id]; !ok || size(e.rows) < n {
				smallest[e.key.id] = size(e.rows)
			}
		}
		firsts := 0
		for _, n := range smallest {
			firsts += n
		}
		cold := c.warm(entries)
		warm := map[int64]bool{}
		for _, e := range entries {
			if c.at[e.key] != nil {
				warm[e.key.id] = true
			}
		}
		if firsts <= c.limit && len(warm) != len(smallest) {
			t.Errorf("round %d: %d of %d lists warm, though their smallest rows (%d bytes) fit in %d", round, len(warm), len(smallest), firsts, c.limit)
		}
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
