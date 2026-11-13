package server

import (
	"bytes"
	"cmp"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/Zeus-Development/proxy-index/api/internal/build"
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

// A purge (Purger) after a full rebuild: purgeTries tries within purgeWindow,
// then purgeSettle after the CDN confirmed it, before the new build is
// announced (announce).
const (
	purgeTries  = 3
	purgeWindow = 60 * time.Second
	purgeSettle = 2 * time.Second
	keyFileMax  = 4096
)

// cdnKey is what a CDN key may hold: printable ASCII without spaces, so it is
// a valid header value.
var cdnKey = regexp.MustCompile(`^[!-~]{1,1024}$`)

// Purger purges the CDN's copies carrying a tag: a POST of {"CacheTag": tag}
// to the pull zone's purge URL with the key in the AccessKey header. The key
// is never logged, printed, returned in an error or shown in /healthz.
type Purger struct {
	url, tag, key  string
	client         *http.Client
	tries          int
	window, settle time.Duration
}

// NewPurger checks purgeURL (https, or http to a loopback host) and reads the
// key from keyFile, which must be private (no access for group or others,
// mode 600) and hold the key alone. An empty tag means DefaultCDNTag.
func NewPurger(purgeURL, keyFile, tag string) (*Purger, error) {
	u, err := url.Parse(purgeURL)
	if err != nil || u.Host == "" || (u.Scheme != "https" && (u.Scheme != "http" || !loopbackHost(u.Hostname()))) {
		return nil, errors.New("CDN purge URL must be an https URL (http only to a loopback host)")
	}
	if err := ValidCDN(0, tag); err != nil {
		return nil, err
	}
	key, err := readKey(keyFile)
	if err != nil {
		return nil, err
	}
	return &Purger{url: purgeURL, tag: cmp.Or(tag, DefaultCDNTag), key: key, tries: purgeTries, window: purgeWindow, settle: purgeSettle,
		client: &http.Client{CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}}, nil
}

func loopbackHost(host string) bool {
	ip := net.ParseIP(host)
	return host == "localhost" || (ip != nil && ip.IsLoopback())
}

// readKey reads a CDN key file: private, at most keyFileMax bytes, one key.
// No error carries the file's content.
func readKey(path string) (string, error) {
	fi, err := os.Stat(path)
	if err != nil {
		return "", fmt.Errorf("CDN key file: %w", err)
	}
	if fi.Mode().Perm()&0o077 != 0 || !fi.Mode().IsRegular() || fi.Size() > keyFileMax {
		return "", fmt.Errorf("CDN key file %s must be a private file (mode 600) of at most %d bytes", path, keyFileMax)
	}
	b, err := os.ReadFile(path)
	if err != nil {
		return "", fmt.Errorf("CDN key file: %w", err)
	}
	key := strings.TrimSpace(string(b))
	if !cdnKey.MatchString(key) {
		return "", fmt.Errorf("CDN key file %s must hold one key of printable characters without spaces", path)
	}
	return key, nil
}

// Purge sends one purge request and reports whether the CDN confirmed it
// (2xx). A redirect is not followed: the key goes to purgeURL only.
func (p *Purger) Purge(ctx context.Context) error {
	body, err := json.Marshal(map[string]string{"CacheTag": p.tag})
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, p.url, bytes.NewReader(body))
	if err != nil {
		return fmt.Errorf("CDN purge: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")
	req.Header.Set("AccessKey", p.key)
	resp, err := p.client.Do(req)
	if err != nil {
		return fmt.Errorf("CDN purge: %w", err)
	}
	io.Copy(io.Discard, io.LimitReader(resp.Body, 64<<10)) //nolint:errcheck // drained for reuse only
	resp.Body.Close()                                      //nolint:errcheck // read only
	if resp.StatusCode/100 != 2 {
		return fmt.Errorf("CDN purge: status %d", resp.StatusCode)
	}
	return nil
}

// purge tries Purge up to p.tries times within p.window: try i starts at
// i*window/tries and lasts at most window/tries. It returns nil at the first
// confirmed purge, else the last error (also when ctx ends).
func (p *Purger) purge(ctx context.Context) error {
	gap := p.window / time.Duration(p.tries)
	start := time.Now()
	for i := 1; ; i++ {
		tctx, cancel := context.WithTimeout(ctx, gap)
		err := p.Purge(tctx)
		cancel()
		if err == nil || i == p.tries {
			return err
		}
		if !sleep(ctx, time.Until(start.Add(time.Duration(i)*gap))) {
			return err
		}
	}
}

// sleep waits d and reports whether ctx is still live.
func sleep(ctx context.Context, d time.Duration) bool {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-t.C:
		return true
	}
}

// CDNHealth is /healthz cdn with a Purger configured: when the last purge
// after a rebuild ended and its error (null when the CDN confirmed it).
type CDNHealth struct {
	LastPurgeAt *time.Time `json:"lastPurgeAt"`
	LastError   *string    `json:"lastError"`
}

// holdAnnounce keeps /v1/version and /v1/dataset naming prev while a full
// rebuild replaces it, when a purge is configured and prev serves: only
// prev's version answer and dataset file are kept, not its build.
func (s *Server) holdAnnounce(prev *Version) {
	if s.opts.Purger == nil || prev == nil {
		return
	}
	s.prevShown.Store(&Version{ID: prev.ID, BuiltAt: prev.BuiltAt, Static: map[string]*Rendered{versionPath: prev.Static[versionPath]},
		Dataset: prev.Dataset, DatasetPath: prev.DatasetPath})
}

// announce purges the CDN's tagged copies after holdAnnounce, waits
// purgeSettle once the CDN confirmed it, then lets /v1/version and
// /v1/dataset name the serving Version v. A purge that fails every try is
// logged and recorded (/healthz cdn) and v is announced anyway; data answers
// already serve v, so nothing waits on the CDN but the announcement.
func (s *Server) announce(ctx context.Context, v *Version) {
	if s.prevShown.Load() == nil {
		return
	}
	defer s.prevShown.Store(nil)
	p := s.opts.Purger
	err := p.purge(ctx)
	s.state.Lock()
	s.state.purgeAt, s.state.purgeError = time.Now(), ""
	if err != nil {
		s.state.purgeError = err.Error()
	}
	s.state.Unlock()
	if err != nil {
		s.log.Error("CDN purge failed; announcing the new version anyway", "version", v.ID, "tag", p.tag, "error", err)
		return
	}
	s.log.Info("CDN purged; announcing the new version", "version", v.ID, "tag", p.tag)
	sleep(ctx, p.settle)
}

// announced is the Version /v1/version and /v1/dataset name: the previous
// one while a purge holds it (holdAnnounce), else the serving one.
func (s *Server) announced() *Version {
	if v := s.prevShown.Load(); v != nil {
		return v
	}
	return s.current.Load()
}

// cdnHealth sets h.CDN with a Purger configured; a failed last purge makes
// the status degraded. s.state is locked.
func (s *Server) cdnHealth(h *Health) {
	if s.opts.Purger == nil {
		return
	}
	h.CDN = &CDNHealth{}
	if !s.state.purgeAt.IsZero() {
		t := s.state.purgeAt
		h.CDN.LastPurgeAt = &t
	}
	if s.state.purgeError != "" {
		e := s.state.purgeError
		h.CDN.LastError, h.Status = &e, "degraded"
	}
}
