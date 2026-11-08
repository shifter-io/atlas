package server

import (
	"fmt"
	"net/http"

	"github.com/Zeus-Development/proxy-index/api/internal/build"
)

// installRegistered installs every registered answer's route
// (api/answers/README.md): Key gives the stored path, which serves as every
// other answer does, or, for an answer with Cut, the body cut on request; a
// request Key refuses answers 400.
func (s *Server) installRegistered(mux *http.ServeMux) {
	for _, a := range build.Answers() {
		mux.HandleFunc(a.Route, func(w http.ResponseWriter, r *http.Request) {
			path, err := a.Key(r)
			if err != nil {
				writeError(w, http.StatusBadRequest, err.Error())
				return
			}
			if a.Cut == nil {
				s.serveStatic(w, r, path)
				return
			}
			v := s.current.Load()
			if v == nil {
				s.unavailable(w)
				return
			}
			s.serveCut(w, r, v, "answer\x00"+path, func() (any, bool, error) {
				body, found := a.Cut(v.Result.Pages.Held[a.Name], path)
				return body, found, nil
			})
		})
	}
}

// registeredFit fails when a registered body is above build.MaxAnswerBytes.
func registeredFit(v *Version) error {
	for path := range v.Result.Pages.Registered {
		if n := len(v.Static[path].Identity); n > build.MaxAnswerBytes {
			return fmt.Errorf("answer %s is %d bytes, above the %d-byte cap", path, n, build.MaxAnswerBytes)
		}
	}
	return nil
}

// checkRegistered asks for every registered answer's samples.
func (c *pageCheck) checkRegistered(pg *build.Pages) {
	for _, a := range build.Answers() {
		why := fmt.Sprintf(" (answer %s)", a.Name)
		if err, ok := pg.Unbuilt[a.Name]; ok {
			why = fmt.Sprintf(" (answer %s: %s)", a.Name, err)
		}
		for _, p := range a.Samples {
			c.check(p, why)
		}
	}
}
