package server

import (
	"fmt"
	"net/http"
	"net/url"
	"runtime/debug"

	"github.com/Zeus-Development/atlas/api/internal/build"
)

// MaxUnserved bounds the unserved pages a build keeps for /healthz and the log.
const MaxUnserved = 50

// The codes of /healthz unserved[] (the unserved set, codes_errors.go).
const (
	UnservedBuildFailed = "build_failed"
	UnservedNotFound    = "not_found"
	UnservedNoNetwork   = "no_network_page"
	UnservedNotServed   = "not_served"
)

// Unserved is one page /healthz lists as unserved: its path (null for a
// published product without a network), why (a code of the unserved set),
// the status it answers (null when it could not be asked) and the published
// product it belongs to (null for an index page or an answer's sample).
type Unserved struct {
	Path    *string `json:"path"`
	Code    string  `json:"code"`
	Status  *int    `json:"status"`
	Product *string `json:"product"`
}

// String is u for the log: "<path> <code> <status> (<product>)", "-" for null.
func (u Unserved) String() string {
	path, status, product := "-", "-", "-"
	if u.Path != nil {
		path = *u.Path
	}
	if u.Status != nil {
		status = fmt.Sprint(*u.Status)
	}
	if u.Product != nil {
		product = *u.Product
	}
	return fmt.Sprintf("%s %s %s (%s)", path, u.Code, status, product)
}

// unservedPages asks the serving version, through the API's own routes, for
// the index pages, every page a published product's network should have and
// every registered answer's samples, and returns each page that does not
// answer 200 (at most MaxUnserved) and the count of the rest. A product with
// no network at all is listed too, unless its provider is hidden on purpose.
func (s *Server) unservedPages(v *Version) ([]Unserved, int) {
	c := &pageCheck{h: s.Handler(), out: []Unserved{}, seen: map[string]bool{}}
	for _, p := range []string{"/v1/networks", "/v1/countries", "/v1/rankings", "/v1/networks/all"} {
		c.check(p, nil, false)
	}
	for _, p := range v.Result.Pages.Published {
		c.product(p)
	}
	c.checkRegistered(v.Result)
	return c.out, c.more
}

// pageCheck collects the unserved pages of one unservedPages run: the first
// MaxUnserved, a count of the rest, and the paths already checked.
type pageCheck struct {
	h    http.Handler
	out  []Unserved
	more int
	seen map[string]bool
}

func (c *pageCheck) add(u Unserved) {
	if len(c.out) < MaxUnserved {
		c.out = append(c.out, u)
	} else {
		c.more++
	}
}

// check asks for path and adds it, with its product, unless it answers 200:
// build_failed when its answer was left out of the build (unbuilt).
func (c *pageCheck) check(path string, product *string, unbuilt bool) {
	u := Unserved{Path: &path, Code: UnservedNotServed, Product: product}
	w := &statusWriter{header: http.Header{}}
	req, err := http.NewRequest(http.MethodHead, path, nil)
	if err != nil {
		c.add(u)
		return
	}
	c.h.ServeHTTP(w, req)
	if w.status == http.StatusOK {
		return
	}
	u.Status = &w.status
	switch {
	case unbuilt:
		u.Code = UnservedBuildFailed
	case w.status == http.StatusNotFound:
		u.Code = UnservedNotFound
	}
	c.add(u)
}

// product checks the network pages of one published product, each network
// page once and its kind's view once per network and kind.
func (c *pageCheck) product(p build.PublishedProduct) {
	switch {
	case p.Hidden:
		return
	case p.Slug == "":
		c.add(Unserved{Code: UnservedNoNetwork, Product: &p.ID})
		return
	case c.seen[p.Slug+"/"+p.Kind]:
		return
	}
	c.seen[p.Slug+"/"+p.Kind] = true
	base := "/v1/networks/" + url.PathEscape(p.Slug)
	for _, path := range []string{base, base + "/chart", base + "/places"} {
		if !c.seen[path] {
			c.seen[path] = true
			c.check(path, &p.ID, false)
		}
		c.check(path+"?kind="+url.QueryEscape(p.Kind), &p.ID, false)
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
