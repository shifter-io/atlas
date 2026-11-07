package build_test

import (
	"bytes"
	"compress/gzip"
	"encoding/json"
	"os"
	"path/filepath"
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
