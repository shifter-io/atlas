package main

import (
	"io"
	"net/http"
	"testing"
)

func TestAdminListensOnLoopbackOnly(t *testing.T) {
	h := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { io.WriteString(w, `{"status":"ok"}`) }) //nolint:errcheck
	for _, addr := range []string{"0.0.0.0:0", "10.0.0.1:8190", ":8190", "example.com:8190", "nonsense"} {
		if _, err := startAdmin(addr, h); err == nil {
			t.Errorf("%s: served", addr)
		}
	}
	if a, err := startAdmin("", h); a != nil || err != nil {
		t.Fatalf("no address: %v %v", a, err)
	}
	a, err := startAdmin("127.0.0.1:0", h)
	if err != nil {
		t.Fatal(err)
	}
	res, err := http.Get("http://" + a.String() + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	defer res.Body.Close()
	if body, _ := io.ReadAll(res.Body); res.StatusCode != http.StatusOK || string(body) != `{"status":"ok"}` {
		t.Errorf("admin: %d %s", res.StatusCode, body)
	}
}

func TestServeAdminListenFlag(t *testing.T) {
	t.Setenv("SITEAPI_ADMIN_LISTEN", "")
	if f := parseServe(nil); f.adminListen != "" {
		t.Errorf("default admin listener %q, want none", f.adminListen)
	}
	if f := parseServe([]string{"--admin-listen", "127.0.0.1:8190"}); f.adminListen != "127.0.0.1:8190" {
		t.Errorf("--admin-listen: %q", f.adminListen)
	}
	t.Setenv("SITEAPI_ADMIN_LISTEN", "127.0.0.1:9190")
	if f := parseServe(nil); f.adminListen != "127.0.0.1:9190" {
		t.Errorf("SITEAPI_ADMIN_LISTEN: %q", f.adminListen)
	}
}

func TestServeHoldFileFlag(t *testing.T) {
	t.Setenv("SITEAPI_HOLD_FILE", "")
	if f := parseServe(nil); f.holdFile != "" {
		t.Errorf("default hold file %q, want none", f.holdFile)
	}
	if f := parseServe([]string{"--hold-file", "/run/hold-blue"}); f.holdFile != "/run/hold-blue" {
		t.Errorf("--hold-file: %q", f.holdFile)
	}
	t.Setenv("SITEAPI_HOLD_FILE", "/run/hold-green")
	if f := parseServe(nil); f.holdFile != "/run/hold-green" {
		t.Errorf("SITEAPI_HOLD_FILE: %q", f.holdFile)
	}
}
