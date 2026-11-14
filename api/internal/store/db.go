// Package store owns every SQL statement of the site API: the `site` schema
// migrations and the read-only queries the cache is built from.
package store

import (
	"context"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// DefaultRequestConns is OpenReadOnly's request reads (siteapi serve
// --db-requests): with them the pool holds 10 connections, half the API
// role's CONNECTION LIMIT, so a restart's old and new processes fit.
const DefaultRequestConns = 2

// OpenReadOnly is OpenReadOnlyFor with DefaultRequestConns.
func OpenReadOnly(ctx context.Context, dsn string) (*pgxpool.Pool, error) {
	return OpenReadOnlyFor(ctx, dsn, DefaultRequestConns)
}

// OpenReadOnlyFor opens the runtime pool. Every session has
// default_transaction_read_only=on, so the serving path cannot write even by
// mistake. It holds a refresh's reads at their most (the load's transaction
// and NetReadConns network list reads; the fingerprint and the events
// refresh run apart from the load), one request's network list read (they
// read one at a time, NetLists), and requests more for the other reads made
// for requests, which the caller bounds to requests at a time
// (server.DBSource.Requests): those never make a refresh wait.
func OpenReadOnlyFor(ctx context.Context, dsn string, requests int) (*pgxpool.Pool, error) {
	cfg, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		return nil, err
	}
	cfg.ConnConfig.RuntimeParams["default_transaction_read_only"] = "on"
	cfg.ConnConfig.RuntimeParams["application_name"] = "siteapi"
	cfg.ConnConfig.RuntimeParams["statement_timeout"] = "30000"
	cfg.MaxConns = int32(2 + NetReadConns + max(requests, 1))
	cfg.MaxConnIdleTime = time.Minute
	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		return nil, err
	}
	if err := pool.Ping(ctx); err != nil {
		pool.Close()
		return nil, fmt.Errorf("connect: %w", err)
	}
	return pool, nil
}

// OpenWrite opens a single read-write connection for migrate and seed commands.
func OpenWrite(ctx context.Context, dsn string) (*pgx.Conn, error) {
	cfg, err := pgx.ParseConfig(dsn)
	if err != nil {
		return nil, err
	}
	cfg.RuntimeParams["application_name"] = "siteapi-admin"
	conn, err := pgx.ConnectConfig(ctx, cfg)
	if err != nil {
		return nil, fmt.Errorf("connect: %w", err)
	}
	return conn, nil
}
