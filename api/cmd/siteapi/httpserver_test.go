package main

import (
	"bufio"
	"fmt"
	"net"
	"net/http"
	"strings"
	"testing"
)

// The API's server sets every timeout and answers headers above 1 MB 431.
func TestHTTPServerSetsEveryTimeoutAndAHeaderLimit(t *testing.T) {
	ok := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {})
	hs := newHTTPServer("127.0.0.1:0", ok)
	if hs.ReadHeaderTimeout <= 0 || hs.ReadTimeout <= 0 || hs.WriteTimeout <= 0 || hs.IdleTimeout <= 0 {
		t.Fatalf("a timeout is unset: %+v", hs)
	}
	if hs.MaxHeaderBytes <= 0 || hs.MaxHeaderBytes > 1<<20 {
		t.Fatalf("MaxHeaderBytes %d, want 1 MB at most", hs.MaxHeaderBytes)
	}
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	go hs.Serve(l) //nolint:errcheck
	defer hs.Close()
	for size, want := range map[int]int{100: 200, 1<<20 + 8<<10: 431} {
		c, err := net.Dial("tcp", l.Addr().String())
		if err != nil {
			t.Fatal(err)
		}
		go fmt.Fprintf(c, "GET / HTTP/1.1\r\nHost: api.test\r\nX-Big: %s\r\nConnection: close\r\n\r\n", strings.Repeat("a", size))
		res, err := http.ReadResponse(bufio.NewReader(c), nil)
		if err != nil {
			t.Fatalf("%d-byte header: %v", size, err)
		}
		if res.StatusCode != want {
			t.Errorf("%d-byte header: %d, want %d", size, res.StatusCode, want)
		}
		c.Close()
	}
}
