package server

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"slices"
	"testing"
	"time"

	"github.com/Zeus-Development/proxy-index/api/internal/store"
)

// shutdownSource is fakeSource whose Load, once armed, signals entered and
// returns its inputs only when its context ends: a shutdown that arrives
// while a rebuild reads.
type shutdownSource struct {
	*fakeSource
	armed   bool
	entered chan struct{}
}

func (s *shutdownSource) Load(ctx context.Context) (*store.Inputs, error) {
	if s.armed {
		close(s.entered)
		<-ctx.Done()
	}
	return s.fakeSource.Load(ctx)
}

// A shutdown during a rebuild (SHUTDOWN-DURING-BUILD-1, deploy 12): Run
// returns within seconds, the rebuild is discarded (the old Version serves,
// no failure is recorded) and it leaves no new or partial dataset file.
func TestShutdownDuringARebuildKeepsTheServingVersion(t *testing.T) {
	t.Parallel()
	src := &shutdownSource{fakeSource: &fakeSource{rev: 1}, entered: make(chan struct{})}
	s := newTestServer(t, src)
	old := s.Current()
	before := dirNames(t, s.opts.FilesDir)
	src.armed = true
	src.set(2, nil)
	ctx, cancel := context.WithCancel(context.Background())
	ran := make(chan struct{})
	go func() { s.Run(ctx); close(ran) }()
	<-src.entered
	cancel()
	select {
	case <-ran:
	case <-time.After(5 * time.Second):
		t.Fatal("Run did not return within 5 s of the shutdown")
	}
	if s.Current() != old {
		t.Fatalf("serving version %d, want the old %d", s.Current().ID, old.ID)
	}
	s.state.Lock()
	lastError, failures := s.state.lastError, s.state.failures
	s.state.Unlock()
	if lastError != "" || failures != 0 {
		t.Fatalf("a stopped rebuild recorded a failure: %d, %q", failures, lastError)
	}
	if after := dirNames(t, s.opts.FilesDir); !slices.Equal(after, before) {
		t.Fatalf("files directory %v, want %v unchanged", after, before)
	}
}

// A dataset file write whose context ends stops and removes its .tmp files.
func TestAStoppedDatasetWriteLeavesNoFile(t *testing.T) {
	t.Parallel()
	ds := newTestServer(t, &fakeSource{rev: 1}).Current().Result.Dataset
	dir := t.TempDir()
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := newDatasetFiles(dir, "code", slog.New(slog.NewTextHandler(io.Discard, nil))).ensure(ctx, ds, "marker", time.Now()); !errors.Is(err, context.Canceled) {
		t.Fatalf("error %v, want context.Canceled", err)
	}
	if names := dirNames(t, dir); len(names) != 0 {
		t.Fatalf("files left: %v", names)
	}
}

// Rendering stops with the context's error once it ends.
func TestRenderVersionStopsWhenItsContextEnds(t *testing.T) {
	t.Parallel()
	s := newTestServer(t, &fakeSource{rev: 1})
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if v, err := renderVersion(ctx, 9, s.Current().Result, s.opts, nil); !errors.Is(err, context.Canceled) || v != nil {
		t.Fatalf("version %v, error %v; want context.Canceled", v != nil, err)
	}
}
