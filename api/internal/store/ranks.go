package store

import (
	"context"
	"time"

	"github.com/jackc/pgx/v5"
)

// RankRow is one network's 30d balanced rank in a kept published build
// older than the current one: the changelog's movers compare a network's
// rank now with its rank at the start of each range (api/answers/changelog.md).
type RankRow struct {
	AsOf    time.Time
	Network string // the head network's id, the snapshot product's ID
	Rank    int32
}

// RankHistoryDays is how far back the rank read goes: the longest range.
const RankHistoryDays = 30

// rankHistorySQL is every ranked head network of each published build of the
// RankHistoryDays before the build $1, oldest first. The calculator keeps one
// published build a day for 30 days (calculated/store_retention.py).
const rankHistorySQL = `SELECT b.as_of, n->>'id', (n->'windows'->'30d'->'scores'->'balanced'->>'rank')::integer
 FROM public.calculation_builds b JOIN public.calculated_snapshots s ON s.build_id = b.build_id
 CROSS JOIN public.calculation_builds cur
 CROSS JOIN LATERAL jsonb_array_elements(CASE WHEN jsonb_typeof(s.payload->'networks') = 'array'
  THEN s.payload->'networks' ELSE '[]'::jsonb END) n
 WHERE cur.build_id = $1 AND b.published AND b.as_of < cur.as_of
  AND b.as_of >= cur.as_of - make_interval(days => $2)
  AND jsonb_typeof(n->'windows'->'30d'->'scores'->'balanced'->'rank') = 'number'
 ORDER BY b.as_of, 2`

// loadRanks reads the rank history (RankRow) of the snapshot's build; none
// without a snapshot.
func (in *Inputs) loadRanks(ctx context.Context, tx pgx.Tx) (err error) {
	if in.Snapshot == nil {
		return nil
	}
	in.Ranks, err = collect(ctx, tx, rankHistorySQL, func(r pgx.Rows, v *RankRow) error {
		return r.Scan(&v.AsOf, &v.Network, &v.Rank)
	}, in.Snapshot.BuildID, RankHistoryDays)
	return err
}
