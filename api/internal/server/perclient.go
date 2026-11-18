package server

import (
	"container/list"
	"errors"
	"fmt"
	"math"
	"net/http"
	"net/netip"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/shifter-io/atlas/api/internal/build"
)

// The per-visitor budget for uncached work (SEC-RATE-1). The guard on
// uncached work (uncached.go) bounds what runs at once, so one visitor can
// still take all of it. Each visitor has a token bucket of ClientBudget.Burst
// tokens that refills at ClientBudget.Rate a second; a request whose answer
// must be computed spends one, and a visitor with none left answers 429
// rate_limited with Retry-After. Cached answers spend nothing.
//
// The visitor is the address the trusted proxy (Caddy) names in
// ClientBudget.Header, on a request from ClientBudget.Trusted only; Caddy
// takes it from the CDN's header only when the request comes from one of the
// CDN's edge servers (scripts/server-setup/, the daily edge list). A request
// from any other address is its own visitor, whatever headers it sends. A
// request from a trusted proxy without the header (a check on the loopback,
// siteapi bench, a Caddy that names no visitor) has no budget: the guard on
// uncached work still bounds it.

const (
	// MaxClients is how many visitors' buckets the budget keeps; a new
	// visitor beyond them drops the one seen longest ago.
	MaxClients = 100_000
	// DefaultClientRate and DefaultClientBurst are siteapi serve's
	// --per-client-rate and --per-client-burst.
	DefaultClientRate  = 5.0
	DefaultClientBurst = 20
	// DefaultClientHeader is the header Caddy names the visitor's address in.
	DefaultClientHeader = "X-Atlas-Client-Ip"
	// DefaultTrustedProxies is siteapi serve's --trusted-proxies: the
	// loopback, where Caddy forwards from.
	DefaultTrustedProxies = "127.0.0.1,::1"
)

// ClientBudget configures the per-visitor budget (Options.Clients). A Rate of
// 0 turns it off.
type ClientBudget struct {
	Rate    float64
	Burst   int
	Header  string
	Trusted []netip.Prefix
}

// Valid reports whether c can configure the budget: Rate is 0 (off) or more
// with a Burst of at least 1.
func (c ClientBudget) Valid() error {
	switch {
	case math.IsNaN(c.Rate) || math.IsInf(c.Rate, 0) || c.Rate < 0:
		return fmt.Errorf("per-client rate %v must be 0 (off) or more", c.Rate)
	case c.Rate > 0 && c.Burst < 1:
		return fmt.Errorf("per-client burst %d must be at least 1", c.Burst)
	}
	return nil
}

// ParseTrustedProxies reads a comma-separated list of addresses and ranges
// (CIDR); an address stands for itself alone. Empty means none.
func ParseTrustedProxies(s string) ([]netip.Prefix, error) {
	var out []netip.Prefix
	for _, f := range strings.Split(s, ",") {
		if f = strings.TrimSpace(f); f == "" {
			continue
		}
		p, err := netip.ParsePrefix(f)
		if err != nil {
			a, aerr := netip.ParseAddr(f)
			if aerr != nil || a.Zone() != "" {
				return nil, fmt.Errorf("trusted proxy %q is no address or range", f)
			}
			p = netip.PrefixFrom(a.Unmap(), a.Unmap().BitLen())
		}
		out = append(out, p.Masked())
	}
	return out, nil
}

// clientBudget is the visitors' buckets, at most limit, in an LRU.
type clientBudget struct {
	rate, burst float64
	limit       int
	mu          sync.Mutex
	order       *list.List // of *bucket, most recently seen first
	byAddr      map[netip.Addr]*list.Element
}

type bucket struct {
	addr   netip.Addr
	tokens float64
	at     time.Time
}

func newClientBudget(rate float64, burst, limit int) *clientBudget {
	return &clientBudget{rate: rate, burst: float64(max(burst, 1)), limit: max(limit, 1), order: list.New(), byAddr: map[netip.Addr]*list.Element{}}
}

// take spends one of addr's tokens at now; when it has none left, wait is how
// long until it has one.
func (b *clientBudget) take(addr netip.Addr, now time.Time) (wait time.Duration, ok bool) {
	b.mu.Lock()
	defer b.mu.Unlock()
	e, seen := b.byAddr[addr]
	if seen {
		b.order.MoveToFront(e)
	} else {
		if b.order.Len() >= b.limit {
			delete(b.byAddr, b.order.Remove(b.order.Back()).(*bucket).addr)
		}
		e = b.order.PushFront(&bucket{addr: addr, tokens: b.burst, at: now})
		b.byAddr[addr] = e
	}
	k := e.Value.(*bucket)
	k.tokens = min(b.burst, k.tokens+max(now.Sub(k.at).Seconds(), 0)*b.rate)
	k.at = now
	if k.tokens >= 1 {
		k.tokens--
		return 0, true
	}
	return time.Duration((1 - k.tokens) / b.rate * float64(time.Second)), false
}

// errNoVisitor is a request from a trusted proxy that names no visitor.
var errNoVisitor = errors.New("the trusted proxy names no visitor")

// clientAddr is the visitor r comes from (the package comment above): its
// bucket's key, an IPv6 address standing for its /64, which one end user
// usually holds whole.
func (s *Server) clientAddr(r *http.Request) (netip.Addr, error) {
	ap, err := netip.ParseAddrPort(r.RemoteAddr)
	if err != nil {
		return netip.Addr{}, err
	}
	addr := ap.Addr().Unmap().WithZone("")
	if s.trustedProxy(addr) {
		v := r.Header.Values(s.opts.Clients.Header)
		if s.opts.Clients.Header == "" || len(v) != 1 {
			return netip.Addr{}, errNoVisitor
		}
		if addr, err = netip.ParseAddr(strings.TrimSpace(v[0])); err != nil {
			return netip.Addr{}, err
		}
		addr = addr.Unmap().WithZone("")
	}
	if addr.Is6() {
		addr = netip.PrefixFrom(addr, 64).Masked().Addr()
	}
	return addr, nil
}

func (s *Server) trustedProxy(addr netip.Addr) bool {
	for _, p := range s.opts.Clients.Trusted {
		if p.Contains(addr) {
			return true
		}
	}
	return false
}

// withinBudget spends a token of r's visitor; when none is left it answers
// 429 rate_limited (no-store, Retry-After in whole seconds, at least 1) and
// returns false. Counted in the admin /healthz limited.
func (s *Server) withinBudget(w http.ResponseWriter, r *http.Request) bool {
	if s.clients == nil {
		return true
	}
	addr, err := s.clientAddr(r)
	if err != nil {
		return true
	}
	wait, ok := s.clients.take(addr, s.opts.Now())
	if ok {
		return true
	}
	s.limitedAnswers.Add(1)
	w.Header().Set("Retry-After", strconv.FormatInt(max(int64(math.Ceil(wait.Seconds())), 1), 10))
	writeError(w, http.StatusTooManyRequests, build.RateLimited())
	return false
}
