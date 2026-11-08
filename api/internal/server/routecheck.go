package server

import (
	"fmt"
	"net/http"
	"net/url"
	"runtime/debug"

	"github.com/Zeus-Development/proxy-index/api/internal/build"
)

// MaxUnserved bounds the unserved pages a build keeps for /healthz and the log.
const MaxUnserved = 50

// unservedPages asks the serving version, through the API's own routes, for
// the index pages, every page a published product's network should have and
// every registered answer's samples, and returns one line per page that does
// not answer 200 (at most MaxUnserved, then a count of the rest). A product with no network at all
// is a line too, unless its provider is hidden on purpose.
func (s *Server) unservedPages(v *Version) []string {
	c := &pageCheck{h: s.Handler(), out: []string{}, seen: map[string]bool{}}
	for _, p := range []string{"/v1/networks", "/v1/countries", "/v1/rankings", "/v1/networks/all"} {
		c.check(p, "")
	}
	for _, p := range v.Result.Pages.Published {
		c.product(p)
	}
	c.checkRegistered(v.Result.Pages)
	if c.more > 0 {
		c.out = append(c.out, fmt.Sprintf("and %d more", c.more))
	}
	return c.out
}

// pageCheck collects the unserved pages of one unservedPages run: the first
// MaxUnserved lines, a count of the rest, and the paths already checked.
type pageCheck struct {
	h    http.Handler
	out  []string
	more int
	seen map[string]bool
}

func (c *pageCheck) add(line string) {
	if len(c.out) < MaxUnserved {
		c.out = append(c.out, line)
	} else {
		c.more++
	}
}

// check asks for path and adds a line, ending in why, unless it answers 200.
func (c *pageCheck) check(path, why string) {
	w := &statusWriter{header: http.Header{}}
	req, err := http.NewRequest(http.MethodHead, path, nil)
	if err != nil {
		c.add(fmt.Sprintf("%s: %v%s", path, err, why))
		return
	}
	c.h.ServeHTTP(w, req)
	if w.status != http.StatusOK {
		c.add(fmt.Sprintf("%s answers %d%s", path, w.status, why))
	}
}

// product checks the network pages of one published product, each network
// page once and its kind's view once per network and kind.
func (c *pageCheck) product(p build.PublishedProduct) {
	switch {
	case p.Hidden:
		return
	case p.Slug == "":
		c.add(fmt.Sprintf("published product %s (provider %s, %s) has no network", p.ID, p.Provider, p.Kind))
		return
	case c.seen[p.Slug+"/"+p.Kind]:
		return
	}
	c.seen[p.Slug+"/"+p.Kind] = true
	why := fmt.Sprintf(" (published product %s)", p.ID)
	base := "/v1/networks/" + url.PathEscape(p.Slug)
	for _, path := range []string{base, base + "/chart", base + "/places"} {
		if !c.seen[path] {
			c.seen[path] = true
			c.check(path, why)
		}
		c.check(path+"?kind="+url.QueryEscape(p.Kind), why)
	}
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
