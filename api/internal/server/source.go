package server

import (
	"context"

	"github.com/Zeus-Development/atlas/api/internal/store"
	"github.com/jackc/pgx/v5/pgxpool"
)

// DBSource reads through a read-only pool; Lists holds the typed lists between loads.
type DBSource struct {
	Pool  *pgxpool.Pool
	Lists *store.Lists
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
	return store.QueryEvents(ctx, d.Pool, f)
}
