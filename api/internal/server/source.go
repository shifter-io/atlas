package server

import (
	"context"

	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/shifter-io/atlas/api/internal/store"
	"golang.org/x/sync/semaphore"
)

// DBSource reads through a read-only pool; Lists holds the typed lists between loads.
// Requests, when set, bounds the reads made for a request (the events
// fallback, Events) to its weight, store.OpenReadOnlyFor's requests: one that
// gets none within BusyWait fails with ErrBusy, so the refresh's reads
// (Fingerprint, Load, LoadEvents and the network lists) always find a
// connection free.
type DBSource struct {
	Pool     *pgxpool.Pool
	Lists    *store.Lists
	Requests *semaphore.Weighted
}

func (d DBSource) Fingerprint(ctx context.Context) (store.Fingerprint, error) {
	return store.ReadFingerprint(ctx, d.Pool)
}

func (d DBSource) Load(ctx context.Context) (*store.Inputs, error) {
	return store.LoadLists(ctx, d.Pool, d.Lists)
}

func (d DBSource) LoadEvents(ctx context.Context) (*store.Inputs, error) {
	return store.LoadEvents(ctx, d.Pool)
}

func (d DBSource) Events(ctx context.Context, f store.EventFilter) ([]store.Event, error) {
	if d.Requests != nil {
		if !acquireWithin(ctx, d.Requests, BusyWait) {
			return nil, ErrBusy
		}
		defer d.Requests.Release(1)
	}
	return store.QueryEvents(ctx, d.Pool, f)
}
