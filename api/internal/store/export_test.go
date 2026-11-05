package store

import (
	"context"

	"github.com/jackc/pgx/v5"
)

// MergeNewest exposes mergeNewest to the external tests.
var MergeNewest = mergeNewest

// ListQuery exposes listQuery to the external tests.
var ListQuery = listQuery

// Pause makes LoadLists call f once it has read the current snapshot row,
// with the calculated_breakdowns lock on or off; undo restores both.
func Pause(f func(), lock bool) (undo func()) {
	afterSnapshot, lockBreakdowns = f, lock
	return func() { afterSnapshot, lockBreakdowns = nil, true }
}

// FailedRead leaves c as a list read that failed after scanning a row of
// place id leaves it: the place created and pending, the lists dropped.
func FailedRead(c *Lists, place int32) {
	c.places, c.networks = map[int32]*Place{}, map[int32]*Network{}
	c.place(place)
	c.held = nil
}

// FetchK2 is c's fetch of the K2 lists ids with every dimension, city x ASN
// included.
func FetchK2(ctx context.Context, c *Lists, tx pgx.Tx, ids []int64) (map[int64]*List, error) {
	withCityAsn := map[int64]bool{}
	for _, id := range ids {
		withCityAsn[id] = true
	}
	return c.fetch(ctx, tx, true, 0, ids, withCityAsn)
}
