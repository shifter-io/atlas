package server

import (
	"errors"
	"net/http"

	"github.com/Zeus-Development/atlas/api/internal/build"
)

// writeError answers status with {"error": {code, params}} (TYPED-ERRORS-1,
// codes_errors.go), uncached.
func writeError(w http.ResponseWriter, status int, e build.APIError) {
	body, _ := Marshal(map[string]build.APIError{"error": e})
	h := w.Header()
	h.Set("Content-Type", "application/json; charset=utf-8")
	h.Set("Cache-Control", CacheNone)
	h.Set("X-Content-Type-Options", "nosniff")
	w.WriteHeader(status)
	w.Write(body) //nolint:errcheck
}

// badRequest answers 400 with err's APIError; an error of another type (no
// Key returns one) answers it as internal_error.
func badRequest(w http.ResponseWriter, err error) {
	e := build.Internal()
	errors.As(err, &e)
	writeError(w, http.StatusBadRequest, e)
}

// notFound answers 404 for the request's path.
func notFound(w http.ResponseWriter, r *http.Request) {
	writeError(w, http.StatusNotFound, build.NotFound(r.URL.Path))
}

// unavailable answers 503: nothing serves yet (cache_not_built), or, while
// the current build is older than payload v4, its reason.
func (s *Server) unavailable(w http.ResponseWriter) {
	s.state.Lock()
	reason := s.state.reason
	s.state.Unlock()
	if reason == "" {
		reason = build.ReasonCacheNotBuilt
	}
	writeError(w, http.StatusServiceUnavailable, build.Unavailable(reason))
}

// maxQuery is the longest q a search, list or events request may send, in
// bytes.
const maxQuery = 200

// renderFailed logs err and answers 500 internal_error: an answer's rows
// could not be read or its body rendered.
func (s *Server) renderFailed(w http.ResponseWriter, r *http.Request, err error) {
	s.log.Error("answer failed", "path", r.URL.Path, "error", err)
	writeError(w, http.StatusInternalServerError, build.Internal())
}
