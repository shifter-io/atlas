package store

import (
	"context"

	"github.com/jackc/pgx/v5"
)

// MergeNewest exposes mergeNewest to the external tests.
var MergeNewest = mergeNewest

// ListQuery exposes listQuery to the external tests.
var ListQuery = listQuery

// FetchK2 is c's fetch of the K2 lists ids with every dimension, city x ASN
// included.
func FetchK2(ctx context.Context, c *Lists, tx pgx.Tx, ids []int64) (map[int64]*List, error) {
	withCityAsn := map[int64]bool{}
	for _, id := range ids {
		withCityAsn[id] = true
	}
	return c.fetch(ctx, tx, true, 0, ids, withCityAsn)
}
