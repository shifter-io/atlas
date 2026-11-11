package store

import (
	"context"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
)

// EventStats is what /v1/events/stats is built from (build/events_stats_answer.go),
// read in the events read's transaction (loadEvents), so on every refresh: the
// test_event_buckets rows (framework schema v13, kept by a trigger on every
// insert into test_events) of the StatsDays before End, and each product's
// newest RecentSignals events in them. No raw event is counted.
type EventStats struct {
	// End is the end of the 30-minute bucket holding the newest event, or the
	// read's now() when that is earlier or there is none (HOME-TAPE-1): live
	// data ends about now, frozen data at its own end. Every range ends there.
	End     time.Time
	Buckets []EventBucket
	Recent  []RecentSignal
	// Missing is true on a framework schema before v13 (no table): no counts.
	Missing bool
}

// EventBucket is one test_event_buckets row. Provider is the product's
// framework provider, nil for events without a product.
type EventBucket struct {
	Start             time.Time
	Provider          *string
	Category          string
	Events, Good, Bad int
	LastAt            time.Time
}

// RecentSignal is one of a product's newest events: its provider, time, id
// and signal.
type RecentSignal struct {
	Provider string
	At       time.Time
	ID       int64
	Signal   int16
}

// StatsDays is the longest range; RecentSignals how many of each network's
// newest signals the answer shows.
const (
	StatsDays     = 30
	RecentSignals = 24
)

const (
	// eventStatsHeadSQL is End and whether test_event_buckets exists; the
	// newest event's at is test_events_at's first entry.
	eventStatsHeadSQL = `SELECT date_bin('30 minutes', least(now(), coalesce((SELECT max(at) FROM public.test_events), now())),
 '2000-01-01 00:00+00') + interval '30 minutes', to_regclass('public.test_event_buckets') IS NOT NULL`
	// eventBucketsSQL reads the buckets from $1 to $2 (at most 1,440 buckets of
	// every product and tab): the primary key's leading bucket_start.
	eventBucketsSQL = `SELECT b.bucket_start, p.provider, b.category, b.events, b.good, b.bad, b.last_at
 FROM public.test_event_buckets b LEFT JOIN public.products p USING (product_id)
 WHERE b.bucket_start >= $1 AND b.bucket_start < $2`
	// recentSignalsSQL reads each product's newest $3 events from $1 to $2, on
	// test_events_product (product_id, at DESC).
	recentSignalsSQL = `SELECT p.provider, r.at, r.event_id, r.signal FROM public.products p CROSS JOIN LATERAL
 (SELECT e.at, e.event_id, e.signal FROM public.test_events e WHERE e.product_id = p.product_id AND e.at >= $1 AND e.at < $2
 ORDER BY e.at DESC, e.event_id DESC LIMIT $3) r`
)

func init() {
	bounds := func() []any {
		end := time.Now().UTC().Truncate(30 * time.Minute).Add(30 * time.Minute)
		return []any{end.AddDate(0, 0, -StatsDays), end}
	}
	RegisterStatement(Statement{"load.event_stats_head", "Inputs.loadEventStats", everyLayout, fixed(eventStatsHeadSQL)})
	RegisterStatement(Statement{"load.event_buckets", "Inputs.loadEventStats", everyLayout, func(*ExplainParams) (string, []any) {
		return eventBucketsSQL, bounds()
	}})
	RegisterStatement(Statement{"load.event_recent", "Inputs.loadEventStats", everyLayout, func(*ExplainParams) (string, []any) {
		return recentSignalsSQL, append(bounds(), RecentSignals)
	}})
}

// loadEventStats reads in.EventStats in tx.
func (in *Inputs) loadEventStats(ctx context.Context, tx pgx.Tx) error {
	s := &in.EventStats
	var exists bool
	if err := tx.QueryRow(ctx, eventStatsHeadSQL).Scan(&s.End, &exists); err != nil {
		return fmt.Errorf("event stats: %w", err)
	}
	s.End, s.Missing = s.End.UTC(), !exists
	if s.Missing {
		return nil
	}
	start := s.End.AddDate(0, 0, -StatsDays)
	var err error
	s.Buckets, err = collect(ctx, tx, eventBucketsSQL, func(r pgx.Rows, b *EventBucket) error {
		return r.Scan(&b.Start, &b.Provider, &b.Category, &b.Events, &b.Good, &b.Bad, &b.LastAt)
	}, start, s.End)
	if err != nil {
		return fmt.Errorf("event buckets: %w", err)
	}
	s.Recent, err = collect(ctx, tx, recentSignalsSQL, func(r pgx.Rows, v *RecentSignal) error {
		return r.Scan(&v.Provider, &v.At, &v.ID, &v.Signal)
	}, start, s.End, RecentSignals)
	if err != nil {
		return fmt.Errorf("recent signals: %w", err)
	}
	return nil
}
