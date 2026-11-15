package server

import (
	"net/http"

	"github.com/Zeus-Development/atlas/api/internal/build"
)

// renderMethodologies precompresses every methodology file of res
// (build.MethodologyFiles), reusing prev's: a file is named by its content
// hash, so the same path is the same bytes. Each is cached for a year and
// immutable (CacheFile), its ETag the methodology id.
func renderMethodologies(res *build.Result, prev *Version) map[string]*Rendered {
	out := make(map[string]*Rendered, len(res.Methodologies))
	for path, body := range res.Methodologies {
		if prev != nil && prev.methodologies[path] != nil {
			out[path] = prev.methodologies[path]
			continue
		}
		out[path] = RenderBytes(http.StatusOK, body, CacheFile, LevelBest)
	}
	return out
}

// serveMethodology answers GET /v1/methodology/<id>.json with the rules
// whose SHA-256 is id, or 404 not_found for an id the API holds no rules for.
func (s *Server) serveMethodology(w http.ResponseWriter, r *http.Request) {
	v := s.current.Load()
	if v == nil {
		s.unavailable(w)
		return
	}
	body, ok := v.methodologies[build.MethodologyPrefix+r.PathValue("name")]
	if !ok {
		notFound(w, r)
		return
	}
	s.write(w, r, v, body)
}
