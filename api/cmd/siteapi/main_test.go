package main

import (
	"flag"
	"strings"
	"testing"
)

func TestFlagUsagesNameProxyIndexFolder(t *testing.T) {
	fs := flag.NewFlagSet("test", flag.ContinueOnError)
	var c common
	c.register(fs)
	f := fs.Lookup("database-file")
	if f == nil {
		t.Fatal("flag database-file is not registered")
	}
	if !strings.Contains(f.Usage, "proxy-index/.secrets/database.toml") {
		t.Errorf("flag database-file usage %q: want proxy-index/.secrets/database.toml", f.Usage)
	}
}
