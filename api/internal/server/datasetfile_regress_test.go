package server

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/shifter-io/atlas/api/internal/store"
)

// Review findings F1 and F2 of API-DATASET-FILE-1. These use only what the
// code had before their fix, so they show the failure before it.

// F1: a dataset file another binary wrote (its meta names other code) is not
// reused after a restart, even for the same marker.
func TestARestartDoesNotReuseAnotherBinarysFile(t *testing.T) {
	t.Parallel()
	now := time.Date(2026, 11, 4, 10, 0, 0, 0, time.UTC)
	dir := t.TempDir()
	src := &fakeSource{rev: 1}
	first := clockServer(t, src, dir, &now).Current().Dataset
	before := stat(t, first)
	metaPath := filepath.Join(dir, datasetMetaName)
	raw, err := os.ReadFile(metaPath)
	if err != nil {
		t.Fatal(err)
	}
	var meta map[string]any
	if err := json.Unmarshal(raw, &meta); err != nil {
		t.Fatal(err)
	}
	meta["code"] = "previous-binary"
	if raw, err = json.Marshal(meta); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(metaPath, raw, 0o644); err != nil {
		t.Fatal(err)
	}
	if f := clockServer(t, src, dir, &now).Current().Dataset; unchanged(t, f, before) {
		t.Fatal("a restart reused the dataset file a previous binary wrote")
	}
}

// F2: a framework product or target change, which changes the dataset's
// targets and providers' products, writes a new dataset file.
func TestAProductOrTargetChangeWritesANewFile(t *testing.T) {
	t.Parallel()
	digest, other := "d", "e"
	base := store.Fingerprint{SiteRevision: 3, Products: 2, TargetsDigest: &digest}
	products, targets := base, base
	products.Products = 3
	targets.TargetsDigest = &other
	for name, fp := range map[string]store.Fingerprint{"products": products, "targets": targets} {
		if datasetMarker(fp) == datasetMarker(base) {
			t.Errorf("a %s change keeps the dataset file", name)
		}
	}
}
