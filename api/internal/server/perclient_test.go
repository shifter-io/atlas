package server

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"net/netip"
	"sync"
	"testing"
	"time"
)

// SEC-RATE-1: a visitor's bucket holds burst tokens, spends one per answer
// computed on request and refills at rate a second, never beyond burst; with
// none left, wait is the time until one is back.
func TestClientBucketSpendsAndRefills(t *testing.T) {
	t.Parallel()
	b := newClientBudget(5, 20, 10)
	a := netip.MustParseAddr("198.51.100.7")
	t0 := time.Unix(1_700_000_000, 0)
	for i := range 20 {
		if _, ok := b.take(a, t0); !ok {
			t.Fatalf("token %d of the burst refused", i+1)
		}
	}
	if wait, ok := b.take(a, t0); ok || wait != 200*time.Millisecond {
		t.Errorf("past the burst: ok %v, wait %v, want refused for 200ms", ok, wait)
	}
	if _, ok := b.take(a, t0.Add(100*time.Millisecond)); ok {
		t.Error("half a token later: allowed")
	}
	if _, ok := b.take(a, t0.Add(300*time.Millisecond)); !ok {
		t.Error("a whole token later: refused")
	}
	// An hour idle refills to the burst, not beyond.
	later := t0.Add(time.Hour)
	for i := range 20 {
		if _, ok := b.take(a, later); !ok {
			t.Fatalf("after an hour, token %d refused", i+1)
		}
	}
	if _, ok := b.take(a, later); ok {
		t.Error("after an hour: more than the burst")
	}
	// A clock that steps back takes no tokens away and adds none.
	if _, ok := b.take(a, t0); ok {
		t.Error("a clock stepping back refilled the bucket")
	}
	if _, ok := b.take(netip.MustParseAddr("198.51.100.8"), later); !ok {
		t.Error("another visitor shares the bucket")
	}
}

// SEC-RATE-1: the budget keeps at most limit visitors; a new one drops the
// visitor seen longest ago, which starts again with a full bucket.
func TestClientBudgetKeepsAtMostLimitVisitors(t *testing.T) {
	t.Parallel()
	b := newClientBudget(1, 1, 3)
	now := time.Unix(1_700_000_000, 0)
	addr := func(i int) netip.Addr { return netip.AddrFrom4([4]byte{198, 51, 100, byte(i)}) }
	for i := 1; i <= 3; i++ {
		b.take(addr(i), now) // each spends its only token
	}
	b.take(addr(1), now) // 1 is now the most recently seen
	b.take(addr(4), now) // drops 2
	if b.order.Len() != 3 || len(b.byAddr) != 3 {
		t.Fatalf("%d visitors (%d in the map), want 3", b.order.Len(), len(b.byAddr))
	}
	if _, ok := b.take(addr(1), now); ok {
		t.Error("visitor 1, recently seen, got a new bucket")
	}
	if _, ok := b.take(addr(2), now); !ok {
		t.Error("visitor 2, dropped, has no full bucket")
	}
	big := newClientBudget(1, 1, MaxClients)
	for i := range MaxClients + 10 {
		big.take(netip.AddrFrom4([4]byte{10, byte(i >> 16), byte(i >> 8), byte(i)}), now)
	}
	if big.order.Len() != MaxClients || len(big.byAddr) != MaxClients {
		t.Errorf("%d visitors after %d, want %d", big.order.Len(), MaxClients+10, MaxClients)
	}
}

// The budget is safe for concurrent requests: n visitors spend their bursts
// at once and every token is spent exactly once.
func TestClientBudgetIsSafeForConcurrentRequests(t *testing.T) {
	t.Parallel()
	b := newClientBudget(1, 5, 100)
	now := time.Unix(1_700_000_000, 0)
	var mu sync.Mutex
	allowed := 0
	var wg sync.WaitGroup
	for i := range 50 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for range 10 {
				if _, ok := b.take(netip.AddrFrom4([4]byte{198, 51, 100, byte(i % 10)}), now); ok {
					mu.Lock()
					allowed++
					mu.Unlock()
				}
			}
		}()
	}
	wg.Wait()
	if allowed != 10*5 {
		t.Errorf("%d answers allowed, want 50 (10 visitors x burst 5)", allowed)
	}
}

func TestParseTrustedProxies(t *testing.T) {
	t.Parallel()
	got, err := ParseTrustedProxies(" 127.0.0.1, ::1 ,192.0.2.0/24,10.1.2.3/8,")
	if err != nil {
		t.Fatal(err)
	}
	if fmt.Sprint(got) != "[127.0.0.1/32 ::1/128 192.0.2.0/24 10.0.0.0/8]" {
		t.Errorf("parsed %v", got)
	}
	if got, err := ParseTrustedProxies(""); err != nil || len(got) != 0 {
		t.Errorf("empty: %v %v, want none", got, err)
	}
	for _, bad := range []string{"localhost", "127.0.0.1/33", "fe80::1%eth0"} {
		if _, err := ParseTrustedProxies(bad); err == nil {
			t.Errorf("%q accepted", bad)
		}
	}
}

func TestClientBudgetValid(t *testing.T) {
	t.Parallel()
	for _, c := range []ClientBudget{{}, {Rate: 5, Burst: 20}, {Rate: 0.1, Burst: 1}} {
		if err := c.Valid(); err != nil {
			t.Errorf("%+v: %v", c, err)
		}
	}
	for _, c := range []ClientBudget{{Rate: -1, Burst: 1}, {Rate: 5}, {Rate: 5, Burst: -1}} {
		if err := c.Valid(); err == nil {
			t.Errorf("%+v accepted", c)
		}
	}
}

// SEC-RATE-1: the visitor is the header's address only on a request from a
// trusted proxy; from any other address the header is ignored. A trusted
// proxy that names no visitor (or names it badly) leaves the request
// without a budget. An IPv6 visitor stands for its /64.
func TestClientAddrTrustsTheHeaderOnlyFromTrustedProxies(t *testing.T) {
	t.Parallel()
	trusted, _ := ParseTrustedProxies(DefaultTrustedProxies)
	s := New(&fakeSource{}, Options{Clients: ClientBudget{Rate: 1, Burst: 1, Header: DefaultClientHeader, Trusted: trusted}})
	none := New(&fakeSource{}, Options{Clients: ClientBudget{Rate: 1, Burst: 1, Trusted: trusted}})
	for _, c := range []struct {
		srv     *Server
		remote  string
		headers []string
		want    string // "" when the request has no budget
	}{
		{s, "127.0.0.1:5000", []string{"198.51.100.7"}, "198.51.100.7"},
		{s, "[::1]:5000", []string{"2001:db8:1:2:3::4"}, "2001:db8:1:2::"},
		{s, "[::ffff:127.0.0.1]:5000", []string{" ::ffff:198.51.100.7 "}, "198.51.100.7"},
		{s, "203.0.113.5:5000", []string{"198.51.100.7"}, "203.0.113.5"},
		{s, "203.0.113.5:5000", nil, "203.0.113.5"},
		{s, "[2001:db8:aa:bb:cc::1]:443", nil, "2001:db8:aa:bb::"},
		{s, "127.0.0.1:5000", nil, ""},
		{s, "127.0.0.1:5000", []string{"198.51.100.7", "198.51.100.8"}, ""},
		{s, "127.0.0.1:5000", []string{"198.51.100.7, 198.51.100.8"}, ""},
		{s, "127.0.0.1:5000", []string{"unknown"}, ""},
		{s, "not-an-address", nil, ""},
		{none, "127.0.0.1:5000", []string{"198.51.100.7"}, ""},
		{none, "203.0.113.5:5000", []string{"198.51.100.7"}, "203.0.113.5"},
	} {
		r := httptest.NewRequest(http.MethodGet, "/v1/search?q=a", nil)
		r.RemoteAddr = c.remote
		for _, v := range c.headers {
			r.Header.Add(DefaultClientHeader, v)
		}
		got, err := c.srv.clientAddr(r)
		if c.want == "" {
			if err == nil {
				t.Errorf("%s %q: visitor %v, want none", c.remote, c.headers, got)
			}
		} else if err != nil || got.String() != c.want {
			t.Errorf("%s %q: %v %v, want %s", c.remote, c.headers, got, err, c.want)
		}
	}
}

// SEC-RATE-1 end to end: past its burst, a visitor's answers computed on
// request answer 429 rate_limited (no-store, Retry-After) until its bucket
// refills; cached answers, other visitors, the page check after a build and
// a loopback request naming no visitor are not limited, and the admin
// /healthz counts the 429s. Without a rate nothing is limited.
func TestVisitorOverItsBudgetAnswers429(t *testing.T) {
	t.Parallel()
	trusted, _ := ParseTrustedProxies(DefaultTrustedProxies)
	var mu sync.Mutex
	now := time.Unix(1_700_000_000, 0)
	clock := func() time.Time { mu.Lock(); defer mu.Unlock(); return now }
	s := New(&fakeSource{rev: 1, events: events(30)}, Options{Logger: slog.New(slog.NewTextHandler(io.Discard, nil)), FilesDir: t.TempDir(), Now: clock,
		Clients: ClientBudget{Rate: 0.25, Burst: 2, Header: DefaultClientHeader, Trusted: trusted}})
	if _, err := s.Refresh(context.Background(), true); err != nil {
		t.Fatal(err)
	}
	h := s.Handler()
	from := func(path, remote string, headers ...string) *httptest.ResponseRecorder {
		r := httptest.NewRequest(http.MethodGet, path, nil)
		r.RemoteAddr = remote
		for i := 0; i+1 < len(headers); i += 2 {
			r.Header.Set(headers[i], headers[i+1])
		}
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, r)
		return rec
	}
	alice := []string{DefaultClientHeader, "198.51.100.7"}
	for _, q := range []string{"alpha", "beta"} {
		if r := from("/v1/search?q="+q, "127.0.0.1:4000", alice...); r.Code != http.StatusOK {
			t.Fatalf("alice's %s within the burst: %d", q, r.Code)
		}
	}
	for _, path := range []string{"/v1/search?q=gamma", "/v1/events?limit=7"} {
		rec := from(path, "127.0.0.1:4000", alice...)
		wantError(t, path, rec, http.StatusTooManyRequests, `{"code":"rate_limited","params":{}}`)
		if got := rec.Header().Get("Retry-After"); got != "4" {
			t.Errorf("%s: Retry-After %q, want 4 (a token every 4 s)", path, got)
		}
	}
	for what, r := range map[string]*httptest.ResponseRecorder{
		"alice's cached search":                 from("/v1/search?q=alpha", "127.0.0.1:4000", alice...),
		"alice's precomputed answer":            from("/v1/networks", "127.0.0.1:4000", alice...),
		"bob":                                   from("/v1/search?q=gamma", "127.0.0.1:4000", DefaultClientHeader, "198.51.100.8"),
		"a loopback request naming no visitor":  from("/v1/search?q=delta", "127.0.0.1:4000"),
		"a direct request claiming alice's":     from("/v1/search?q=epsilon", "203.0.113.5:4000", DefaultClientHeader, "198.51.100.7"),
		"the page check's own request as alice": withOwnCheck(h, "/v1/search?q=zeta", alice...),
	} {
		if r.Code != http.StatusOK {
			t.Errorf("%s: %d, want 200", what, r.Code)
		}
	}
	mu.Lock()
	now = now.Add(4 * time.Second)
	mu.Unlock()
	if r := from("/v1/search?q=eta", "127.0.0.1:4000", alice...); r.Code != http.StatusOK {
		t.Errorf("alice after her bucket refilled a token: %d", r.Code)
	}
	if hl, _ := s.health(); hl.Limited != 2 || hl.Busy != 0 {
		t.Errorf("/healthz limited %d busy %d, want 2 and 0", hl.Limited, hl.Busy)
	}
	off := guardedServer(t, &fakeSource{rev: 1, events: events(30)}, 2)
	for i := range 50 {
		if r := get(off.Handler(), fmt.Sprintf("/v1/search?q=q%d", i)); r.Code != http.StatusOK {
			t.Fatalf("no per-visitor rate, search %d: %d", i, r.Code)
		}
	}
}

// withOwnCheck serves path as one of the page check's own requests.
func withOwnCheck(h http.Handler, path string, headers ...string) *httptest.ResponseRecorder {
	r := httptest.NewRequest(http.MethodGet, path, nil)
	r.RemoteAddr = "127.0.0.1:4000"
	for i := 0; i+1 < len(headers); i += 2 {
		r.Header.Set(headers[i], headers[i+1])
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, r.WithContext(context.WithValue(r.Context(), ownCheck{}, true)))
	return rec
}
