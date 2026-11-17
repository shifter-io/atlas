package main

import (
	"context"
	"io"
	"log/slog"
	"os"
	"sync/atomic"
	"testing"
	"time"

	"github.com/shifter-io/atlas/api/internal/server"
	"github.com/shifter-io/atlas/api/internal/store"
	"github.com/shifter-io/atlas/api/internal/testscale"
)

// slowSource builds testscale inputs. Its fingerprint changes with every
// load, so the refresh loop rebuilds; the second load signals entered, then
// takes until its context ends (when honour) or hold passes.
type slowSource struct {
	in      *store.Inputs
	loads   atomic.Int64
	honour  bool
	hold    time.Duration
	entered chan struct{}
}

func (s *slowSource) Fingerprint(context.Context) (store.Fingerprint, error) {
	return store.Fingerprint{SiteRevision: s.loads.Load() + 1}, nil
}

func (s *slowSource) Load(ctx context.Context) (*store.Inputs, error) {
	if s.loads.Add(1) == 2 {
		close(s.entered)
		done := ctx.Done()
		if !s.honour {
			done = nil
		}
		select {
		case <-done:
		case <-time.After(s.hold):
		}
	}
	in := *s.in
	in.Fingerprint.SiteRevision = s.loads.Load()
	return &in, nil
}

func (s *slowSource) LoadEvents(context.Context) (*store.Inputs, error) { return &store.Inputs{}, nil }

func (s *slowSource) Events(context.Context, store.EventFilter) ([]store.Event, error) {
	return nil, nil
}

// serveStopped starts serveUntilDone on a server that is rebuilding, ends its
// context once the rebuild's load has started and returns how long
// serveUntilDone then took, with the server and its files directory.
func serveStopped(t *testing.T, src *slowSource) (time.Duration, *server.Server, string) {
	t.Helper()
	in, err := testscale.Inputs(testscale.Shape{Networks: 2, Countries: 2, Exits: 3, Tail: 5})
	if err != nil {
		t.Fatal(err)
	}
	src.in, src.entered = in, make(chan struct{})
	dir := t.TempDir()
	srv := server.New(src, server.Options{Interval: 10 * time.Millisecond, FilesDir: dir, Logger: slog.New(slog.NewTextHandler(io.Discard, nil))})
	if _, err := srv.Refresh(context.Background(), true); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- serveUntilDone(ctx, srv, "127.0.0.1:0") }()
	<-src.entered
	start := time.Now()
	cancel()
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	return time.Since(start), srv, dir
}

// SIGTERM during a rebuild (deploy 12): serve returns within 5 s, the old
// version intact and no partial dataset file in the files directory.
func TestServeReturnsSoonWhenStoppedDuringARebuild(t *testing.T) {
	took, srv, dir := serveStopped(t, &slowSource{honour: true, hold: time.Minute})
	if took > 5*time.Second {
		t.Fatalf("serve took %v to return after the shutdown, want at most 5 s", took)
	}
	if v := srv.Current(); v == nil || v.ID != 1 {
		t.Fatalf("serving version %v, want the first build", v)
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 4 { // the first build's file, .gz, .br and the meta
		names := []string{}
		for _, e := range entries {
			names = append(names, e.Name())
		}
		t.Fatalf("files directory %v, want only the first build's", names)
	}
}

// A rebuild that does not stop delays the exit by at most stopWait.
func TestServeWaitsForARebuildAtMostStopWait(t *testing.T) {
	stopWait = 50 * time.Millisecond
	defer func() { stopWait = 5 * time.Second }()
	took, _, _ := serveStopped(t, &slowSource{hold: 3 * time.Second})
	if took > 2*time.Second {
		t.Fatalf("serve took %v to return, want about stopWait", took)
	}
}
