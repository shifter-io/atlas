package server

import (
	"net/http"

	"golang.org/x/sync/semaphore"
)

// RoutePatterns is s's route patterns and the pattern a request takes, for
// the external tests that must reach every route.
func RoutePatterns(s *Server) (patterns []string, pattern func(*http.Request) string) {
	m := s.routes()
	return m.patterns, func(r *http.Request) string {
		_, p := m.Handler(r)
		return p
	}
}

// LockedBuffer is lockedBuffer, a log sink safe for concurrent handlers.
type LockedBuffer = lockedBuffer

// SetUncachedLimit sets s's limit of uncached work (Options.UncachedLimit)
// and gives its DBSource requests connections for request reads
// (DBSource.Requests), as siteapi serve does; before s serves.
func SetUncachedLimit(s *Server, limit, requests int) {
	s.work = semaphore.NewWeighted(int64(limit))
	if d, ok := s.src.(DBSource); ok {
		d.Requests = semaphore.NewWeighted(int64(requests))
		s.src = d
	}
}
