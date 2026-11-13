package server

import (
	"cmp"
	"fmt"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/Zeus-Development/atlas/api/internal/build"
)

// With a CDN configured (Options.CDNTTL > 0), data answers are kept at the
// CDN for CDNTTL whatever the data schedule, and a new build reaches readers
// by a purge of the CDN's copies carrying CDNTag, not by their expiry.
// Browsers keep a data answer for cdnBrowserAge only, so a purge shows within
// a minute. Every other answer class (version, events, the dataset file, old
// dataset names, /healthz, errors) keeps its own Cache-Control. Without a CDN
// the data answers are cached until the next data update (cache.go).
const (
	// DefaultCDNTag is the purge tag data answers carry (CDN-Tag).
	DefaultCDNTag = "atlas-data"
	cdnBrowserAge = 60 * time.Second
)

// cdnTag is what a purge tag may hold: a header-safe word.
var cdnTag = regexp.MustCompile(`^[A-Za-z0-9._-]{1,64}$`)

// ValidCDN reports whether ttl and tag can configure the CDN policy: ttl is 0
// (no CDN) or at least a second, and tag is a word of letters, digits, '.',
// '_' and '-' (empty means DefaultCDNTag).
func ValidCDN(ttl time.Duration, tag string) error {
	if ttl < 0 || (ttl > 0 && ttl < time.Second) {
		return fmt.Errorf("CDN TTL %v must be 0 (off) or at least 1s", ttl)
	}
	if tag != "" && !cdnTag.MatchString(tag) {
		return fmt.Errorf("CDN tag %q must be 1 to 64 letters, digits, '.', '_' or '-'", tag)
	}
	return nil
}

// cdnCache sets a data answer's headers when a CDN is configured: the CDN
// keeps it for ttl (whole seconds), a browser for cdnBrowserAge, and CDN-Tag
// names it for a purge.
func cdnCache(h http.Header, now time.Time, ttl time.Duration, tag string) {
	browser := int64(cdnBrowserAge / time.Second)
	h.Set("Cache-Control", "public, max-age="+strconv.FormatInt(browser, 10)+", s-maxage="+strconv.FormatInt(max(int64(ttl/time.Second), 1), 10))
	h.Set("Expires", now.Add(cdnBrowserAge).UTC().Format(http.TimeFormat))
	h.Set("CDN-Tag", cmp.Or(tag, DefaultCDNTag))
}

// cdnNoParams are the routes that take no query parameter yet, unlike the
// others, accepted any: behind a CDN that varies its copies by query string,
// each new value would fetch the answer (the dataset file the largest) from
// the API again.
var cdnNoParams = []string{"/v1/coverage", "/v1/coverage/", versionPath, "/v1/dataset", "/v1/files/"}

// refuseParams answers 400 to any query (even "?&") on cdnNoParams when a CDN
// is configured, and passes every other request to next unchanged.
func (s *Server) refuseParams(next http.Handler) http.Handler {
	if s.opts.CDNTTL <= 0 {
		return next
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.RawQuery != "" && takesNoParams(r.URL.Path) {
			if onlyParams(w, r.URL.Query()) { // a query without a name, such as "?&"
				badRequest(w, build.UnknownParameter(r.URL.RawQuery))
			}
			return
		}
		next.ServeHTTP(w, r)
	})
}

// takesNoParams reports whether path is one of cdnNoParams (an entry ending
// in "/" covers the paths under it).
func takesNoParams(path string) bool {
	for _, p := range cdnNoParams {
		if path == p || (strings.HasSuffix(p, "/") && strings.HasPrefix(path, p)) {
			return true
		}
	}
	return false
}
