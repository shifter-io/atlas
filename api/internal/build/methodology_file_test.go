package build_test

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"slices"
	"testing"

	"github.com/Zeus-Development/atlas/api/internal/build"
	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/store"
)

// methodologyFile is configs/methodology.toml as policy.load() hashes it,
// written by Python (internal/pyjson/testdata), and its methodology id.
func methodologyFile(t *testing.T) ([]byte, string) {
	t.Helper()
	body, err := os.ReadFile("../pyjson/testdata/methodology.json")
	if err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(body)
	return body, hex.EncodeToString(sum[:])
}

// A methodology row gets a file only when its canonical definition hashes
// to its id: the file's bytes are then the ones the id is the SHA-256 of,
// whatever layout the database printed the definition in.
func TestMethodologyFilesHashToTheirIDs(t *testing.T) {
	t.Parallel()
	body, id := methodologyFile(t)
	var spaced bytes.Buffer
	if err := json.Indent(&spaced, body, "", " "); err != nil {
		t.Fatal(err)
	}
	other := "0000000000000000000000000000000000000000000000000000000000000000"
	files := build.MethodologyFiles([]store.MethodologyRow{
		{ID: id, Definition: spaced.Bytes()},
		{ID: other, Definition: body},           // the definition of another id
		{ID: "broken", Definition: []byte("{")}, // not JSON
	})
	if len(files) != 1 {
		t.Fatalf("files %v, want only %s's", len(files), id)
	}
	got := files[build.MethodologyPath(id)]
	sum := sha256.Sum256(got)
	if !bytes.Equal(got, body) || hex.EncodeToString(sum[:]) != id {
		t.Errorf("file of %s is %s", id, got)
	}
	if build.MethodologyPath(id) != "/v1/methodology/"+id+".json" {
		t.Errorf("path %s", build.MethodologyPath(id))
	}
}

// withMethodologyID is a snapshot fixture published under methodology id.
func withMethodologyID(t *testing.T, name, id string) []byte {
	t.Helper()
	var s map[string]any
	if err := json.Unmarshal(fixtureSnapshot(t, name, nil), &s); err != nil {
		t.Fatal(err)
	}
	s["methodology"].(map[string]any)["id"] = id
	body, err := json.Marshal(s)
	if err != nil {
		t.Fatal(err)
	}
	return body
}

// Through the database (jsonb), a published snapshot whose methodology row
// gives back its rules names their file in /v1/meta and the dataset's
// snapshot, and the file is the bytes the id hashes; a snapshot whose row
// does not (the fixtures' ids are not their settings' hash) has a null
// methodologyUrl, in na as methodology_file_unknown.
func TestTheSnapshotNamesItsMethodologyFile(t *testing.T) {
	t.Parallel()
	body, id := methodologyFile(t)
	db := catalogued(t)
	db.Exec(t, "INSERT INTO methodologies VALUES ($1, $2::jsonb)", id, string(body))
	db.PublishV4(t, withMethodologyID(t, "snapshot.json", id))
	res := buildFrom(t, db)
	path := build.MethodologyPath(id)
	m := metaOf(t, res)
	if !bytes.Equal(res.Methodologies[path], body) {
		t.Errorf("file %s is %s", path, res.Methodologies[path])
	}
	if model.SV(m.Snapshot.MethodologyURL) != path || model.SV(res.Dataset.Snapshot.MethodologyURL) != path ||
		slices.Contains(m.NA, "snapshot.methodologyUrl") || slices.Contains(res.Dataset.NA, "snapshot.methodologyUrl") {
		t.Errorf("meta %+v na %v, dataset %+v na %v", m.Snapshot, m.NA, res.Dataset.Snapshot, res.Dataset.NA)
	}

	unknown := metaOf(t, publishedBuild(t, fixtureSnapshot(t, "snapshot.json", nil)))
	if unknown.Snapshot.MethodologyURL != nil || unknown.Snapshot.MethodologyID == nil ||
		unknown.ReasonOf("snapshot.methodologyUrl") != build.NAMethodologyFileUnknown {
		t.Errorf("snapshot %+v, na %v %v", unknown.Snapshot, unknown.NA, unknown.Reasons)
	}
}
