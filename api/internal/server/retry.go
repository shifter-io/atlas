package server

import (
	"errors"
	"time"

	"github.com/Zeus-Development/atlas/api/internal/snap"
	"github.com/Zeus-Development/atlas/api/internal/store"
)

// MaxRetryWait caps the doubling wait before a fingerprint whose build failed
// with a transient error is built again.
const MaxRetryWait = 30 * time.Minute

// Refused marks a build error that the same inputs repeat: an answer over
// build.MaxAnswerBytes or a content check of build.Build that fails.
// store.ErrBeforeV4 is one too. Refresh does not build such a fingerprint
// again while its data part is unchanged; a new fingerprint or a restart does.
type Refused struct{ Err error }

func (r *Refused) Error() string { return r.Err.Error() }
func (r *Refused) Unwrap() error { return r.Err }

// refused reports whether err is a deterministic refusal (Refused).
func refused(err error) bool {
	var r *Refused
	return errors.As(err, &r) || errors.Is(err, store.ErrBeforeV4)
}

// failedBuild is the last failed full build: the fingerprint it read, its
// error and when, how many builds of that fingerprint failed in a row, and
// when it may be built again (none when refused).
type failedBuild struct {
	fp      store.Fingerprint
	err     string
	at      time.Time
	tries   int
	refused bool
	retryAt time.Time
}

// held reports whether the last failed build read fp's data part and must
// not be built again yet: it was refused, or its wait has not ended.
func (s *Server) held(fp store.Fingerprint) bool {
	s.state.Lock()
	defer s.state.Unlock()
	f := s.state.failed
	if f == nil || f.fp.DataKey() != fp.DataKey() {
		return false
	}
	return f.refused || s.opts.Now().Before(f.retryAt)
}

// buildFailed remembers that building fp failed with err. A refusal is not
// retried; any other error is retried at the next poll the first time, then
// after a wait that doubles from one poll interval up to MaxRetryWait.
func (s *Server) buildFailed(fp store.Fingerprint, err error) {
	now := s.opts.Now()
	s.state.Lock()
	f := s.state.failed
	if f == nil || f.fp.DataKey() != fp.DataKey() {
		f = &failedBuild{fp: fp}
		s.state.failed = f
	}
	f.err, f.at, f.refused = err.Error(), now, refused(err)
	f.tries++
	f.retryAt = now.Add(retryWait(s.opts.Interval, f.tries))
	tries, retryAt, ref := f.tries, f.retryAt, f.refused
	s.state.Unlock()
	if ref {
		s.log.Warn("build refused; not retried until the fingerprint changes", "build_id", fp.BuildID, "error", err)
		return
	}
	s.log.Warn("build failed; retrying", "build_id", fp.BuildID, "tries", tries, "retry_at", retryAt)
}

// retryWait is the wait after the tries-th failed build of one fingerprint:
// none after the first (the next poll), then 1, 2, 4 and so on poll
// intervals, at most MaxRetryWait.
func retryWait(interval time.Duration, tries int) time.Duration {
	if tries <= 1 {
		return 0
	}
	w := interval
	for i := 2; i < tries && w < MaxRetryWait; i++ {
		w *= 2
	}
	return min(w, MaxRetryWait)
}

// forgetFailed drops the failed build: the serving Version answers the
// fingerprint again, or a build succeeded.
func (s *Server) forgetFailed() {
	s.state.Lock()
	s.state.failed = nil
	s.state.Unlock()
}

// failedHealth fills /healthz's failed build fields; the caller holds
// s.state. A failed build makes the status degraded.
func (s *Server) failedHealth(h *Health) {
	f := s.state.failed
	if f == nil {
		return
	}
	e := f.err
	h.FailedFingerprint, h.FailedError, h.FailedAt = f.fp.BuildID, &e, snap.At(f.at)
	if !f.refused {
		h.RetryAt = snap.At(f.retryAt)
	}
	h.Status = "degraded"
}
