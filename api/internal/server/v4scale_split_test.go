package server

import (
	"encoding/json"
	"maps"
	"os"
	"testing"
)

// The split golden files hold exactly the hashes of the one file they replace
// (PAGE-ANSWERS-1): this PR's first run reads the old file once; the next
// commit removes it with this test.
func TestTheSplitV4GoldenKeepsEveryHash(t *testing.T) {
	t.Parallel()
	raw, err := os.ReadFile("testdata/answers-v4-scale.json")
	if err != nil {
		t.Fatal(err)
	}
	var old map[string]map[string]string
	if err := json.Unmarshal(raw, &old); err != nil {
		t.Fatal(err)
	}
	got := readV4Golden(t)
	if len(got) != len(old) {
		t.Fatalf("%d cases, %d before", len(got), len(old))
	}
	for c, answers := range old {
		if !maps.Equal(got[c], answers) {
			t.Errorf("case %s: %d hashes differ from the old file's %d", c, len(got[c]), len(answers))
		}
	}
}
