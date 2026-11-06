package main

import (
	"flag"
	"strings"
	"testing"
)

func TestFlagUsagesNameRepositorySecrets(t *testing.T) {
	fs := flag.NewFlagSet("test", flag.ContinueOnError)
	var c common
	c.register(fs)
	f := fs.Lookup("database-file")
	if f == nil {
		t.Fatal("flag database-file is not registered")
	}
	if !strings.Contains(f.Usage, ".secrets/database.toml in the repository root") {
		t.Errorf("flag database-file usage %q: want .secrets/database.toml in the repository root", f.Usage)
	}
}
