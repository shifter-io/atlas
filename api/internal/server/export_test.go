package server

import "net/http"

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
