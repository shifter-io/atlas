package main

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
)

// cdnKeyFile writes a private key file for the fake CDN.
func cdnKeyFile(t *testing.T) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "cdn-key")
	if err := os.WriteFile(path, []byte("cli-test-key\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestCDNPurgePurgesOnceAndFailsOnARefusal(t *testing.T) {
	var status atomic.Int64
	var got atomic.Int64
	cdn := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodPost && r.Header.Get("AccessKey") == "cli-test-key" {
			got.Add(1)
		}
		w.WriteHeader(int(status.Load()))
	}))
	defer cdn.Close()
	key := cdnKeyFile(t)
	args := []string{"--cdn-purge-url", cdn.URL + "/purge", "--cdn-key-file", key, "--cdn-tag", "atlas-staging"}
	status.Store(http.StatusNoContent)
	if err := cdnPurge(args); err != nil || got.Load() != 1 {
		t.Fatalf("cdn-purge = %v after %d requests, want a success after 1", err, got.Load())
	}
	status.Store(http.StatusUnauthorized)
	if err := cdnPurge(args); err == nil || got.Load() != 2 || strings.Contains(err.Error(), "cli-test-key") {
		t.Fatalf("a refused cdn-purge = %v after %d requests, want an error without the key after 2", err, got.Load())
	}
	for _, bad := range [][]string{nil, {"--cdn-purge-url", cdn.URL}, {"--cdn-key-file", key}} {
		if err := cdnPurge(bad); err == nil {
			t.Errorf("cdn-purge %v: accepted", bad)
		}
	}
}

func TestServePurgeFlagsGoTogether(t *testing.T) {
	key := cdnKeyFile(t)
	for _, c := range []struct {
		f    purgeFlags
		set  bool
		fail bool
	}{
		{purgeFlags{}, false, false},
		{purgeFlags{purgeURL: "https://cdn.example/purge", keyFile: key}, true, false},
		{purgeFlags{purgeURL: "https://cdn.example/purge"}, false, true},
		{purgeFlags{keyFile: key}, false, true},
	} {
		p, err := c.f.purger("atlas-data")
		if (p != nil) != c.set || (err != nil) != c.fail {
			t.Errorf("%+v: purger %v, error %v", c.f, p != nil, err)
		}
	}
	t.Setenv("SITEAPI_CDN_PURGE_URL", "https://cdn.example/purge")
	t.Setenv("SITEAPI_CDN_KEY_FILE", key)
	if f := parseServe(nil); f.purgeURL != "https://cdn.example/purge" || f.keyFile != key {
		t.Errorf("serve purge flags from the environment: %+v", f.purgeFlags)
	}
}
