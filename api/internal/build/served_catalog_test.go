package build

import (
	"encoding/json"
	"testing"
)

// A plan's catalog is served without its English copy (features,
// poolDescription; #962) and with every other key; one that is not a JSON
// object is served as it is.
func TestServedCatalogDropsItsProse(t *testing.T) {
	t.Parallel()
	for _, c := range []struct{ name, in, want string }{
		{"prose and values", `{"pool":"Full Geo","features":["HTTP(S) + SOCKS5"],"unitRate":2,"poolDescription":"205M+ IPs","tier":"Lite"}`,
			`{"pool":"Full Geo","tier":"Lite","unitRate":2}`},
		{"no prose", `{"unitRate":2,"originalPrice":25}`, `{"originalPrice":25,"unitRate":2}`},
		{"prose only", `{"features":[],"poolDescription":"x"}`, `{}`},
		{"not an object", `["a","b"]`, `["a","b"]`},
		{"null", `null`, `null`},
		{"empty", ``, ``},
	} {
		got := servedCatalog(json.RawMessage(c.in))
		if !sameCatalog(t, got, c.want) {
			t.Errorf("%s: %s, want %s", c.name, got, c.want)
		}
	}
}

// sameCatalog is whether got and want are the same JSON value (both empty
// when want is).
func sameCatalog(t *testing.T, got json.RawMessage, want string) bool {
	t.Helper()
	if want == "" || len(got) == 0 {
		return want == "" && len(got) == 0
	}
	var g, w any
	if err := json.Unmarshal(got, &g); err != nil {
		t.Fatalf("%s: %v", got, err)
	}
	if err := json.Unmarshal([]byte(want), &w); err != nil {
		t.Fatal(err)
	}
	gb, err := json.Marshal(g)
	if err != nil {
		t.Fatal(err)
	}
	wb, err := json.Marshal(w)
	if err != nil {
		t.Fatal(err)
	}
	return string(gb) == string(wb)
}
