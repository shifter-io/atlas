package build_test

import (
	"bytes"
	"compress/gzip"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"testing"
)

// golden compares a test's answers with its golden name (readGolden), or
// writes them as the golden when SITEAPI_UPDATE_GOLDENS is set.
func golden(t *testing.T, name string, got map[string][]byte) {
	t.Helper()
	if os.Getenv("SITEAPI_UPDATE_GOLDENS") == "" {
		compareAnswers(t, "golden "+name, readGolden(t, name), got)
		return
	}
	answers := make(map[string]string, len(got))
	for k, v := range got {
		answers[k] = string(v)
	}
	b, err := json.Marshal(answers)
	if err != nil {
		t.Fatal(err)
	}
	var file bytes.Buffer
	z := gzip.NewWriter(&file)
	if _, err = z.Write(b); err == nil {
		err = z.Close()
	}
	if err == nil {
		err = os.WriteFile(goldenFile(name), file.Bytes(), 0o644)
	}
	if err != nil {
		t.Fatal(err)
	}
}

// goldenFile is the file of golden name: gzipped JSON, an object of every
// answer by name, each a JSON string.
func goldenFile(name string) string {
	return filepath.Join("testdata", name+".golden.json.gz")
}

// readGolden is the answers of golden name.
func readGolden(t *testing.T, name string) map[string][]byte {
	t.Helper()
	f, err := os.Open(goldenFile(name))
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	z, err := gzip.NewReader(f)
	if err != nil {
		t.Fatal(err)
	}
	var answers map[string]string
	if err := json.NewDecoder(z).Decode(&answers); err != nil {
		t.Fatal(err)
	}
	out := make(map[string][]byte, len(answers))
	for k, v := range answers {
		out[k] = []byte(v)
	}
	return out
}

// diff lists, up to 40, the fields where actual differs from expected
// (generic JSON values, so numbers are json.Number).
func diff(expected, actual any, path string, diffs *[]string) {
	if len(*diffs) > 40 {
		return
	}
	switch e := expected.(type) {
	case map[string]any:
		a, ok := actual.(map[string]any)
		if !ok {
			*diffs = append(*diffs, fmt.Sprintf("%s: want object, got %v", path, actual))
			return
		}
		for k, ev := range e {
			if av, ok := a[k]; ok {
				diff(ev, av, path+"."+k, diffs)
			} else {
				*diffs = append(*diffs, fmt.Sprintf("%s.%s: missing", path, k))
			}
		}
		for k := range a {
			if _, ok := e[k]; !ok {
				*diffs = append(*diffs, fmt.Sprintf("%s.%s: unexpected", path, k))
			}
		}
	case []any:
		a, ok := actual.([]any)
		if !ok || len(a) != len(e) {
			*diffs = append(*diffs, fmt.Sprintf("%s: want array of %d, got %v", path, len(e), actual))
			return
		}
		for i := range e {
			diff(e[i], a[i], fmt.Sprintf("%s[%d]", path, i), diffs)
		}
	default:
		if !reflect.DeepEqual(expected, actual) {
			*diffs = append(*diffs, fmt.Sprintf("%s: want %v, got %v", path, expected, actual))
		}
	}
}
