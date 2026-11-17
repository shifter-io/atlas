package server

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"net/http"
	"os"
	"testing"

	"github.com/Zeus-Development/atlas/api/internal/build"
	"github.com/Zeus-Development/atlas/api/internal/store"
	"github.com/andybalholm/brotli"
)

// methodologySource is fakeSource with methodology rows.
type methodologySource struct {
	*fakeSource
	rows []store.MethodologyRow
}

func (m methodologySource) Load(ctx context.Context) (*store.Inputs, error) {
	in, err := m.fakeSource.Load(ctx)
	if in != nil {
		in.Methodologies = m.rows
	}
	return in, err
}

// GET /v1/methodology/<id>.json answers the rules whose SHA-256 is id, as
// JSON cached for a year (immutable, its ETag the id), in every encoding and
// with 304 on a match; an id the API holds no rules for, a definition that
// does not hash to its id or a query parameter does not answer them.
func TestMethodologyFileIsTheBytesItsIDHashes(t *testing.T) {
	t.Parallel()
	body, err := os.ReadFile("../pyjson/testdata/methodology.json")
	if err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(body)
	id := hex.EncodeToString(sum[:])
	wrong := "1111111111111111111111111111111111111111111111111111111111111111"
	src := methodologySource{&fakeSource{rev: 1}, []store.MethodologyRow{{ID: id, Definition: body}, {ID: wrong, Definition: body}}}
	s := newTestServer(t, src)
	h, path := s.Handler(), build.MethodologyPath(id)

	rec := get(h, path)
	got := sha256.Sum256(rec.Body.Bytes())
	if rec.Code != http.StatusOK || hex.EncodeToString(got[:]) != id {
		t.Fatalf("%s: %d, body %s", path, rec.Code, rec.Body)
	}
	if hdr := rec.Header(); hdr.Get("Cache-Control") != CacheFile || hdr.Get("ETag") != `"`+id+`"` ||
		hdr.Get("Content-Type") != "application/json; charset=utf-8" {
		t.Errorf("headers %v", hdr)
	}
	if rec := get(h, path, "If-None-Match", `"`+id+`"`); rec.Code != http.StatusNotModified {
		t.Errorf("If-None-Match: %d", rec.Code)
	}
	br := get(h, path, "Accept-Encoding", "br")
	if plain, err := io.ReadAll(brotli.NewReader(br.Body)); err != nil || !bytes.Equal(plain, body) {
		t.Errorf("brotli body differs (%v)", err)
	}

	for p, want := range map[string]int{
		build.MethodologyPath(wrong): http.StatusNotFound, // its definition is another id's
		build.MethodologyPath("00"):  http.StatusNotFound,
		"/v1/methodology/" + id:      http.StatusNotFound, // no .json
		path + "?x=1":                http.StatusBadRequest,
	} {
		rec := get(h, p)
		var e struct {
			Error build.APIError `json:"error"`
		}
		if rec.Code != want || json.Unmarshal(rec.Body.Bytes(), &e) != nil || e.Error.Code == "" {
			t.Errorf("%s: %d %s, want %d with a typed error", p, rec.Code, rec.Body, want)
		}
	}

	// A rebuild keeps the rendered file: the same path is the same bytes.
	first := s.current.Load().methodologies[path]
	src.set(2, nil)
	if _, err := s.Refresh(context.Background(), false); err != nil {
		t.Fatal(err)
	}
	if v := s.current.Load(); v.ID < 2 || v.methodologies[path] != first {
		t.Errorf("version %d did not reuse the file", v.ID)
	}
}

// Before the first build there is no file to answer: 503, as every route.
func TestMethodologyFileWaitsForTheFirstBuild(t *testing.T) {
	t.Parallel()
	s := New(&fakeSource{}, Options{})
	if rec := get(s.Handler(), build.MethodologyPath("00")); rec.Code != http.StatusServiceUnavailable {
		t.Errorf("%d %s", rec.Code, rec.Body)
	}
}
