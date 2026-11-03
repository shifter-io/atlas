package server

import (
	"fmt"
	"net/http"
	"net/url"
	"runtime/debug"
)

// MaxUnserved bounds the unserved pages a build keeps for /healthz and the log.
const MaxUnserved = 50

// unservedPages asks the serving version, through the API's own routes, for
// the index pages and every page a published product's network should have,
// and returns one line per page that does not answer 200 (at most
// MaxUnserved, then a count of the rest). A product with no network at all
// is a line too, unless its provider is hidden on purpose.
func (s *Server) unservedPages(v *Version) []string {
	h := s.Handler()
	out, more := []string{}, 0
	add := func(line string) {
		if len(out) < MaxUnserved {
			out = append(out, line)
		} else {
			more++
		}
	}
	check := func(path, why string) {
		w := &statusWriter{header: http.Header{}}
		req, err := http.NewRequest(http.MethodHead, path, nil)
		if err != nil {
			add(fmt.Sprintf("%s: %v%s", path, err, why))
			return
		}
		h.ServeHTTP(w, req)
		if w.status != http.StatusOK {
			add(fmt.Sprintf("%s answers %d%s", path, w.status, why))
		}
	}
	for _, p := range []string{"/v1/networks", "/v1/countries", "/v1/rankings", "/v1/networks/all"} {
		check(p, "")
	}
	seen := map[string]bool{}
	for _, p := range v.Result.Pages.Published {
		switch {
		case p.Hidden:
			continue
		case p.Slug == "":
			add(fmt.Sprintf("published product %s (provider %s, %s) has no network", p.ID, p.Provider, p.Kind))
			continue
		case seen[p.Slug+"/"+p.Kind]:
			continue
		}
		seen[p.Slug+"/"+p.Kind] = true
		why := fmt.Sprintf(" (published product %s)", p.ID)
		base := "/v1/networks/" + url.PathEscape(p.Slug)
		for _, path := range []string{base, base + "/chart", base + "/places"} {
			if !seen[path] {
				seen[path] = true
				check(path, why)
			}
			check(path+"?kind="+url.QueryEscape(p.Kind), why)
		}
	}
	if more > 0 {
		out = append(out, fmt.Sprintf("and %d more", more))
	}
	return out
}

func (w *statusWriter) Header() http.Header { return w.header }

func (w *statusWriter) Write(b []byte) (int, error) {
	if w.status == 0 {
		w.status = http.StatusOK
	}
	return len(b), nil
}

func (w *statusWriter) WriteHeader(code int) {
	if w.status == 0 {
		w.status = code
	}
}

// statusWriter keeps only the status of a response.
type statusWriter struct {
	header http.Header
	status int
}

// Binary is the source revision this siteapi was built from, as the Go
// toolchain recorded it (null fields when built outside a git checkout).
type Binary struct {
	Revision *string `json:"revision"`
	Time     *string `json:"time"`
	Modified *bool   `json:"modified"`
}

// BinaryInfo reads the running binary's build information.
func BinaryInfo() Binary {
	var b Binary
	info, ok := debug.ReadBuildInfo()
	if !ok {
		return b
	}
	for _, s := range info.Settings {
		v := s.Value
		switch s.Key {
		case "vcs.revision":
			b.Revision = &v
		case "vcs.time":
			b.Time = &v
		case "vcs.modified":
			m := v == "true"
			b.Modified = &m
		}
	}
	return b
}
