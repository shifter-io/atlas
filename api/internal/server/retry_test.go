package server

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/Zeus-Development/atlas/api/internal/build"
	"github.com/Zeus-Development/atlas/api/internal/store"
)

// idSource is fakeSource with the revision as the fingerprint's build id too,
// so /healthz failedFingerprint names it.
type idSource struct{ *fakeSource }

func (s idSource) Fingerprint(ctx context.Context) (store.Fingerprint, error) {
	fp, err := s.fakeSource.Fingerprint(ctx)
	id := fp.SiteRevision
	fp.BuildID = &id
	return fp, err
}

func (s idSource) Load(ctx context.Context) (*store.Inputs, error) {
	in, err := s.fakeSource.Load(ctx)
	if in != nil {
		id := in.Fingerprint.SiteRevision
		in.Fingerprint.BuildID = &id
	}
	return in, err
}

// fakeClock is Options.Now, moved by the test; as a Purger's clock
// (cdn_purge_test.go) its waits move it too and are recorded.
type fakeClock struct {
	mu    sync.Mutex
	now   time.Time
	waits []time.Duration
}

func (c *fakeClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.now
}

func (c *fakeClock) add(d time.Duration) {
	c.mu.Lock()
	c.now = c.now.Add(d)
	c.mu.Unlock()
}

const retryInterval = 10 * time.Second

// newRetryServer serves revision 1 of an idSource on a fake clock.
func newRetryServer(t *testing.T) (*Server, *fakeSource, *fakeClock) {
	t.Helper()
	src := &fakeSource{rev: 1}
	clock := &fakeClock{now: time.Date(2026, 11, 12, 12, 0, 0, 0, time.UTC)}
	s := New(idSource{src}, Options{Interval: retryInterval, Logger: slog.New(slog.NewTextHandler(io.Discard, nil)),
		FilesDir: t.TempDir(), Now: clock.Now})
	if _, err := s.Refresh(context.Background(), true); err != nil {
		t.Fatal(err)
	}
	return s, src, clock
}

// poll is one tick of Run: the clock moves one interval, then Refresh.
func poll(s *Server, clock *fakeClock) {
	clock.add(retryInterval)
	s.Refresh(context.Background(), false) //nolint:errcheck
}

func TestRefusedBuildIsAttemptedOnce(t *testing.T) {
	t.Parallel()
	s, src, clock := newRetryServer(t)
	before := get(s.Handler(), "/v1/dataset").Body.String()
	src.set(2, &Refused{errors.New("answer /v1/home/7d is 205565 bytes, above the 204800-byte cap")})
	loads := src.loads.Load()
	for range 200 { // over half an hour of polls
		poll(s, clock)
		if get(s.Handler(), "/v1/dataset").Body.String() != before {
			t.Fatal("the served version changed during a refusal")
		}
	}
	if n := src.loads.Load() - loads; n != 1 {
		t.Fatalf("a refused build was attempted %d times, want 1", n)
	}
	h := health(t, s)
	if h.Status != "degraded" || h.Version != 1 || h.FailedFingerprint == nil || *h.FailedFingerprint != 2 || h.RetryAt != nil ||
		h.FailedError == nil || !strings.Contains(*h.FailedError, "above the 204800-byte cap") || h.FailedAt == nil {
		t.Fatalf("health during a refusal %+v", h)
	}
	// Force (serve's first build) and a restart try again.
	if _, err := s.Refresh(context.Background(), true); err == nil || src.loads.Load()-loads != 2 {
		t.Fatalf("force did not build again: %v, %d loads", err, src.loads.Load()-loads)
	}
	if _, err := New(idSource{src}, Options{FilesDir: t.TempDir(), Logger: s.log}).Refresh(context.Background(), false); err == nil {
		t.Fatal("a restarted server did not try the refused build")
	}
	// A new fingerprint is attempted at once and clears the failure.
	src.set(3, nil)
	if ok, err := s.Refresh(context.Background(), false); !ok || err != nil {
		t.Fatalf("new fingerprint not built: %v %v", ok, err)
	}
	if h := health(t, s); h.Status != "ok" || h.FailedFingerprint != nil || h.FailedError != nil || h.FailedAt != nil || h.RetryAt != nil {
		t.Fatalf("health after the next build %+v", h)
	}
}

func TestBeforeV4IsNotRetried(t *testing.T) {
	t.Parallel()
	s, src, clock := newRetryServer(t)
	src.set(2, fmt.Errorf("snapshot 2: %w", store.ErrBeforeV4))
	loads := src.loads.Load()
	for range 20 {
		poll(s, clock)
	}
	if n := src.loads.Load() - loads; n != 1 {
		t.Fatalf("a build older than v4 was attempted %d times, want 1", n)
	}
	if h := health(t, s); h.Status != "unavailable" || h.Reason == nil || h.RetryAt != nil || h.FailedFingerprint == nil {
		t.Fatalf("health %+v", h)
	}
}

func TestTransientFailureRetriesWithADoublingWait(t *testing.T) {
	t.Parallel()
	s, src, clock := newRetryServer(t)
	before := get(s.Handler(), "/v1/dataset").Body.String()
	src.set(2, errors.New("database unreachable"))
	var attempts []int
	for i := range 40 {
		n := src.loads.Load()
		poll(s, clock)
		if src.loads.Load() != n {
			attempts = append(attempts, i)
		}
		if get(s.Handler(), "/v1/dataset").Body.String() != before {
			t.Fatal("the served version changed during a failure")
		}
	}
	// The next poll, then a wait of 1, 2, 4 and 8 intervals.
	if want := []int{0, 1, 2, 4, 8, 16, 32}; fmt.Sprint(attempts) != fmt.Sprint(want) {
		t.Fatalf("attempts at polls %v, want %v", attempts, want)
	}
	h := health(t, s)
	if h.Status != "degraded" || h.RetryAt == nil || h.FailedAt == nil || time.Time(*h.RetryAt).Sub(time.Time(*h.FailedAt)) != 32*retryInterval ||
		*h.FailedFingerprint != 2 || !strings.Contains(*h.FailedError, "database unreachable") {
		t.Fatalf("health during the wait %+v", h)
	}
	// A new fingerprint is attempted at once, with a new wait.
	src.set(3, errors.New("database unreachable"))
	n := src.loads.Load()
	poll(s, clock)
	poll(s, clock)
	if src.loads.Load()-n != 2 {
		t.Fatalf("a new fingerprint did not start a new wait: %d loads", src.loads.Load()-n)
	}
	// A success clears it.
	src.set(3, nil)
	clock.add(retryInterval)
	if ok, err := s.Refresh(context.Background(), false); !ok || err != nil {
		t.Fatalf("recovery not built: %v %v", ok, err)
	}
	if h := health(t, s); h.Status != "ok" || h.FailedFingerprint != nil || h.RetryAt != nil {
		t.Fatalf("health after recovery %+v", h)
	}
}

func TestRetryWait(t *testing.T) {
	t.Parallel()
	for _, c := range []struct {
		interval time.Duration
		tries    int
		want     time.Duration
	}{
		{10 * time.Second, 1, 0}, {10 * time.Second, 2, 10 * time.Second}, {10 * time.Second, 3, 20 * time.Second},
		{10 * time.Second, 4, 40 * time.Second}, {10 * time.Second, 9, 1280 * time.Second}, {10 * time.Second, 10, MaxRetryWait},
		{10 * time.Second, 1000, MaxRetryWait}, {time.Minute, 7, MaxRetryWait},
	} {
		if got := retryWait(c.interval, c.tries); got != c.want {
			t.Errorf("retryWait(%v, %d) = %v, want %v", c.interval, c.tries, got, c.want)
		}
	}
}

func TestRefusalsAreTyped(t *testing.T) {
	t.Parallel()
	path := "/v1/home/7d"
	v := &Version{Result: &build.Result{Pages: &build.Pages{Registered: map[string]any{path: nil}}},
		Static: map[string]*Rendered{path: {Identity: make([]byte, build.MaxAnswerBytes+1)}}}
	if err := registeredFit(v); err == nil || !refused(err) {
		t.Fatalf("an answer over the cap is not a refusal: %v", err)
	}
	if !refused(fmt.Errorf("load: %w", store.ErrBeforeV4)) || refused(fmt.Errorf("load: %w", context.DeadlineExceeded)) {
		t.Fatal("refused classifies ErrBeforeV4 or a timeout wrong")
	}
	// A build.Build error is a content check: refused.
	s := New(badSnapshot{&fakeSource{}}, Options{FilesDir: t.TempDir(), Logger: slog.New(slog.NewTextHandler(io.Discard, nil))})
	if _, err := s.build(context.Background(), nil); err == nil || !refused(err) {
		t.Fatalf("a build.Build error is not a refusal: %v", err)
	}
}

// badSnapshot loads a published snapshot build.Build cannot read.
type badSnapshot struct{ *fakeSource }

func (badSnapshot) Load(context.Context) (*store.Inputs, error) {
	in := fixture(1, nil, false)
	in.Snapshot = &store.SnapshotRow{BuildID: 1, Payload: []byte("not json")}
	return in, nil
}
