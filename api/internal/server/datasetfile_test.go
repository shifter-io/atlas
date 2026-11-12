package server

import (
	"bytes"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"runtime/debug"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/Zeus-Development/proxy-index/api/internal/build"
	"github.com/Zeus-Development/proxy-index/api/internal/store"
	"github.com/Zeus-Development/proxy-index/api/internal/testscale"
	"github.com/andybalholm/brotli"
)

// fileBytes is a dataset file's identity bytes, read from disk.
func fileBytes(t testing.TB, f *DatasetFile) []byte {
	t.Helper()
	b, err := os.ReadFile(filepath.Join(f.dir, f.Name()))
	if err != nil {
		t.Fatal(err)
	}
	return b
}

// testCode is the CodeID of clockServer's code.
const testCode = "test-build"

// clockServer is a server over src writing to dir, on the clock *now, as
// code testCode.
func clockServer(t *testing.T, src Source, dir string, now *time.Time) *Server {
	t.Helper()
	return codeServer(t, src, dir, now, testCode)
}

func codeServer(t *testing.T, src Source, dir string, now *time.Time, code string) *Server {
	t.Helper()
	s := New(src, Options{Logger: slog.New(slog.NewTextHandler(io.Discard, nil)), FilesDir: dir, Now: func() time.Time { return *now },
		CodeID: code})
	if _, err := s.Refresh(context.Background(), true); err != nil {
		t.Fatal(err)
	}
	return s
}

// dirNames is the files directory's entries.
func dirNames(t *testing.T, dir string) []string {
	t.Helper()
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	out := []string{}
	for _, e := range entries {
		out = append(out, e.Name())
	}
	return out
}

func fileNames(fs ...*DatasetFile) []string {
	out := []string{datasetMetaName}
	for _, f := range fs {
		out = append(out, f.Name(), f.Name()+".br", f.Name()+".gz")
	}
	slices.Sort(out)
	return out
}

func unchanged(t *testing.T, f *DatasetFile, before os.FileInfo) bool {
	t.Helper()
	after, err := os.Stat(filepath.Join(f.dir, f.Name()))
	return err == nil && os.SameFile(before, after) && after.ModTime().Equal(before.ModTime())
}

func stat(t *testing.T, f *DatasetFile) os.FileInfo {
	t.Helper()
	st, err := os.Stat(filepath.Join(f.dir, f.Name()))
	if err != nil {
		t.Fatal(err)
	}
	return st
}

// The file's bytes are today's dataset encoding (Marshal of the whole
// Dataset), its codings decode to them, and nothing else is left behind.
func TestTheDatasetFileHasTheDatasetsBytes(t *testing.T) {
	t.Parallel()
	s := newTestServer(t, &fakeSource{rev: 1, events: events(5), complete: true})
	v := s.Current()
	want, err := Marshal(v.Result.Dataset)
	if err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(want)
	f := v.Dataset
	if got := fileBytes(t, f); !bytes.Equal(got, want) || f.SHA256 != hex.EncodeToString(sum[:]) || f.Bytes != int64(len(want)) {
		t.Fatalf("file %d bytes, sha %s; want %d bytes", f.Bytes, f.SHA256, len(want))
	}
	gz, _ := os.ReadFile(filepath.Join(f.dir, f.Name()+".gz"))
	zr, err := gzip.NewReader(bytes.NewReader(gz))
	if err != nil {
		t.Fatal(err)
	}
	if dec, _ := io.ReadAll(zr); !bytes.Equal(dec, want) || int64(len(gz)) != f.Gzip {
		t.Fatal("the .gz does not decode to the dataset")
	}
	br, _ := os.ReadFile(filepath.Join(f.dir, f.Name()+".br"))
	if dec, _ := io.ReadAll(brotli.NewReader(bytes.NewReader(br))); !bytes.Equal(dec, want) || int64(len(br)) != f.Brotli {
		t.Fatal("the .br does not decode to the dataset")
	}
	if got := dirNames(t, f.dir); !slices.Equal(got, fileNames(f)) {
		t.Fatalf("files directory %v", got)
	}
}

// The marker is the build's created_at, the site tables' last change (their
// newest updated_at and the revision their triggers bump, which also counts
// deletes) and the framework's products and targets the dataset shows; events
// and which site tables exist are not part of it.
func TestTheMarkerIsTheBuildAndTheSiteTables(t *testing.T) {
	t.Parallel()
	at := time.Date(2026, 11, 4, 10, 0, 0, 0, time.UTC)
	later := at.Add(time.Hour)
	id, digest := int64(7), "d"
	base := store.Fingerprint{BuildID: &id, BuildCreatedAt: &at, SiteRevision: 3, SiteUpdatedAt: &at, Products: 2, TargetsDigest: &digest}
	same := []func(*store.Fingerprint){
		func(f *store.Fingerprint) { f.MaxEventID = &id },
		func(f *store.Fingerprint) { f.SiteTables = "providers" },
	}
	changed := []func(*store.Fingerprint){
		func(f *store.Fingerprint) { f.BuildCreatedAt = &later },
		func(f *store.Fingerprint) { f.SiteUpdatedAt = &later },
		func(f *store.Fingerprint) { f.SiteRevision = 4 },
		func(f *store.Fingerprint) { f.Products = 3 },
		func(f *store.Fingerprint) { other := "e"; f.TargetsDigest = &other },
	}
	for i, m := range append(same, changed...) {
		fp := base
		m(&fp)
		if differs := datasetMarker(fp) != datasetMarker(base); differs != (i >= len(same)) {
			t.Errorf("change %d: marker differs %v", i, differs)
		}
	}
}

// A rebuild for a new event alone (forced here; the real fingerprint only
// refreshes the events answers) keeps the file as it is on disk; a new marker
// writes a new one.
func TestTheDatasetFileIsWrittenOnlyWhenTheMarkerChanges(t *testing.T) {
	t.Parallel()
	src := &fakeSource{rev: 1, complete: true}
	s := newTestServer(t, src)
	first := s.Current().Dataset
	before := stat(t, first)
	src.mu.Lock()
	src.events = events(3)
	src.mu.Unlock()
	if _, err := s.Refresh(context.Background(), true); err != nil {
		t.Fatal(err)
	}
	if v := s.Current(); v.Dataset != first || !unchanged(t, first, before) || len(v.Result.Events.Raw) == 0 {
		t.Fatal("a rebuild for events wrote the dataset file again")
	}
	src.set(2, nil)
	if _, err := s.Refresh(context.Background(), false); err != nil {
		t.Fatal(err)
	}
	if next := s.Current().Dataset; next == first || next.SHA256 == first.SHA256 {
		t.Fatal("a new marker kept the old file")
	}
}

// A replaced file answers under its own name for datasetGrace after the
// replacement, each file counted from its own; then its name redirects to the
// current file and its files are deleted.
func TestAReplacedFileAnswersUntilItsGraceEnds(t *testing.T) {
	t.Parallel()
	now := time.Date(2026, 11, 4, 10, 0, 0, 0, time.UTC)
	src := &fakeSource{rev: 1}
	s := clockServer(t, src, t.TempDir(), &now)
	h := s.Handler()
	rebuild := func(rev int64, at time.Duration) *DatasetFile {
		src.set(rev, nil)
		now = now.Add(at)
		if _, err := s.Refresh(context.Background(), false); err != nil {
			t.Fatal(err)
		}
		return s.Current().Dataset
	}
	f1 := s.Current().Dataset
	b1 := fileBytes(t, f1)
	f2 := rebuild(2, 0)
	f3 := rebuild(3, time.Minute)
	for _, at := range []time.Duration{0, datasetGrace - time.Minute - time.Second} {
		now = now.Add(at)
		s.Refresh(context.Background(), false) //nolint:errcheck // nothing changed: only the grace check
		if r := getOnce(h, f1.Path()); r.Code != 200 || !bytes.Equal(r.Body.Bytes(), b1) || getOnce(h, f2.Path()).Code != 200 {
			t.Fatalf("within the grace: %d", r.Code)
		}
	}
	now = now.Add(2 * time.Second)
	s.Refresh(context.Background(), false) //nolint:errcheck
	moved := getOnce(h, f1.Path())
	if moved.Code != http.StatusFound || moved.Header().Get("Location") != f3.Name() || moved.Header().Get("Cache-Control") != CacheNone {
		t.Fatalf("after the grace: %d %v", moved.Code, moved.Header())
	}
	if getOnce(h, f2.Path()).Code != 200 {
		t.Fatal("the second file's grace ended with the first one's")
	}
	if got := dirNames(t, f3.dir); !slices.Equal(got, fileNames(f2, f3)) {
		t.Fatalf("files directory after the first grace %v", got)
	}
	now = now.Add(time.Minute)
	s.Refresh(context.Background(), false) //nolint:errcheck
	if got := dirNames(t, f3.dir); !slices.Equal(got, fileNames(f3)) || getOnce(h, f2.Path()).Code != http.StatusFound {
		t.Fatalf("files directory after both graces %v", got)
	}
}

// A restart reuses the file whose meta marker matches (and the replaced files
// still in their grace) and removes what an interrupted write left; a damaged
// file or another marker is written again.
func TestARestartReusesTheFileWhoseMarkerMatches(t *testing.T) {
	t.Parallel()
	now := time.Date(2026, 11, 4, 10, 0, 0, 0, time.UTC)
	dir := t.TempDir()
	src := &fakeSource{rev: 1}
	f1 := clockServer(t, src, dir, &now).Current().Dataset
	src.set(2, nil)
	f2 := clockServer(t, src, dir, &now).Current().Dataset
	before := stat(t, f2)
	for _, name := range []string{"dataset-123.json.tmp", "notes.txt"} {
		if err := os.WriteFile(filepath.Join(dir, name), []byte("x"), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	now = now.Add(time.Minute)
	s := clockServer(t, src, dir, &now)
	if f := s.Current().Dataset; f.SHA256 != f2.SHA256 || !unchanged(t, f, before) {
		t.Fatal("a restart wrote the file again")
	}
	if r := getOnce(s.Handler(), f1.Path()); r.Code != 200 {
		t.Fatalf("the replaced file after a restart within its grace: %d", r.Code)
	}
	if got := dirNames(t, dir); !slices.Equal(got, append(fileNames(f1, f2), "notes.txt")) {
		t.Fatalf("files directory after a restart %v", got)
	}
	if err := os.Truncate(filepath.Join(dir, f2.Name()+".br"), 1); err != nil {
		t.Fatal(err)
	}
	if f := clockServer(t, src, dir, &now).Current().Dataset; f.SHA256 != f2.SHA256 || unchanged(t, f, before) {
		t.Fatal("a damaged file was reused")
	}
	src.set(3, nil)
	if f := clockServer(t, src, dir, &now).Current().Dataset; f.SHA256 == f2.SHA256 {
		t.Fatal("another marker reused the file")
	}
	if err := os.WriteFile(filepath.Join(dir, datasetMetaName), []byte("{"), 0o644); err != nil {
		t.Fatal(err)
	}
	if f := clockServer(t, src, dir, &now).Current().Dataset; f.SHA256 == f2.SHA256 || len(fileBytes(t, f)) == 0 {
		t.Fatal("an unreadable meta file was not replaced")
	}
}

// A restart reuses the file only when the same, known code wrote it: other
// code, or code whose revision is unknown or modified (CodeID ""), writes it
// again (here with the same bytes, so the same name and a new file on disk).
func TestARestartReusesOnlyTheSameCodesFile(t *testing.T) {
	t.Parallel()
	now := time.Date(2026, 11, 4, 10, 0, 0, 0, time.UTC)
	dir := t.TempDir()
	src := &fakeSource{rev: 1}
	first := clockServer(t, src, dir, &now).Current().Dataset
	before := stat(t, first)
	for _, code := range []string{"other-build", ""} {
		f := codeServer(t, src, dir, &now, code).Current().Dataset
		if f.SHA256 != first.SHA256 || unchanged(t, f, before) {
			t.Fatalf("code %q reused the file other code wrote", code)
		}
		before = stat(t, f)
	}
	rev, clean, modified := "abc", false, true
	for _, c := range []struct {
		b    Binary
		want string
	}{{Binary{}, ""}, {Binary{Revision: &rev}, ""}, {Binary{Revision: &rev, Modified: &modified}, ""}, {Binary{Revision: &rev, Modified: &clean}, "abc"}} {
		if got := CodeID(c.b); got != c.want {
			t.Errorf("CodeID(%v, %v) = %q, want %q", c.b.Revision != nil, c.b.Modified, got, c.want)
		}
	}
}

// Without a files directory, or with one that cannot be written, a refresh
// fails and the serving version (if any) stays; a file gone from disk answers
// 503 instead of a broken body.
func TestDatasetFileFailures(t *testing.T) {
	t.Parallel()
	if _, err := New(&fakeSource{rev: 1}, Options{Logger: slog.New(slog.NewTextHandler(io.Discard, nil))}).Refresh(context.Background(), true); err == nil ||
		!strings.Contains(err.Error(), "FilesDir") {
		t.Fatalf("no files directory: %v", err)
	}
	dir := t.TempDir()
	src := &fakeSource{rev: 1}
	s := newTestServer(t, src)
	s.files = newDatasetFiles(filepath.Join(dir, "missing"), testCode, s.log)
	v := s.Current()
	src.set(2, nil)
	if _, err := s.Refresh(context.Background(), false); err == nil || s.Current() != v {
		t.Fatalf("an unwritable files directory: %v", err)
	}
	if err := os.Remove(filepath.Join(v.Dataset.dir, v.Dataset.Name()+".gz")); err != nil {
		t.Fatal(err)
	}
	if r := getOnce(s.Handler(), v.DatasetPath, "Accept-Encoding", "gzip"); r.Code != http.StatusServiceUnavailable || r.Header().Get("ETag") != "" {
		t.Fatalf("a missing file: %d %v", r.Code, r.Header())
	}
	if _, err := RenderVersion(1, v.Result, Options{FilesDir: filepath.Join(dir, "missing")}); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("RenderVersion into a missing directory: %v", err)
	}
	// A server without a files directory serves a stored version's file and
	// redirects other dataset names to it.
	bare := New(&fakeSource{}, Options{})
	bare.current.Store(v)
	if r := getOnce(bare.Handler(), "/v1/files/dataset-"+strings.Repeat("a", 64)+".json"); r.Code != http.StatusFound {
		t.Fatalf("another dataset name without a files directory: %d", r.Code)
	}
}

// memoryShape is a small synthetic build whose networks carry QA-1's
// per-network content (240 exit countries per window, the browser targets,
// 559 events) with only 4 requested countries each, so it builds in a fraction
// of QA-1's memory (QA-1's inputs alone need about 3.5 GB, the whole of a
// 4 GB CI runner). Its providers are repeated until the dataset is at least
// memoryDatasetBytes: a stand-in for 18 networks x 249 countries, which the
// generator cannot build within a test machine's memory. A network's dataset
// entry holds its kinds, windows and exit countries, not one part per
// requested country, so the repeated file overstates the real one.
var memoryShape = testscale.Shape{Networks: 18, Countries: 4, Exits: 240, Tail: 5, Unmeasured: 3, Events: 559, Browser: true}

// memoryDatasetBytes is the stand-in dataset's minimum size: about the size of
// QA-1's dataset with every provider repeated ceil(249/40) = 7 times.
const memoryDatasetBytes = 90_000_000

// datasetWriteBound is the most heap writing a dataset file may add: one
// provider's encoding, the buffers and both compressors' state (brotli's is
// about 110 MB), whatever the file's size.
const datasetWriteBound = 160 << 20

// datasetJSONBound is the most heap encoding the dataset's JSON may add: one
// provider's encoding (the largest QA-1 provider is about 0.7 MB) and the
// buffer, far below the file itself.
const datasetJSONBound = 32 << 20

// heapPeak runs fn and returns the largest heap growth seen over the heap
// before it, sampled every millisecond. The collector runs at GOGC=10 meanwhile,
// so the figure is what fn keeps alive, not garbage awaiting a collection.
func heapPeak(fn func()) uint64 {
	defer debug.SetGCPercent(debug.SetGCPercent(10))
	var m runtime.MemStats
	runtime.GC()
	runtime.ReadMemStats(&m)
	base, peak := m.HeapAlloc, m.HeapAlloc
	done, stopped := make(chan struct{}), make(chan struct{})
	go func() {
		defer close(stopped)
		var s runtime.MemStats
		for {
			runtime.ReadMemStats(&s)
			peak = max(peak, s.HeapAlloc)
			select {
			case <-done:
				return
			case <-time.After(time.Millisecond):
			}
		}
	}()
	fn()
	close(done)
	<-stopped
	return peak - base
}

// Writing the dataset file never holds the file: encoding the JSON of a file
// of at least 90 MB adds at most datasetJSONBound of heap (holding the file would
// take its whole size), and the whole write, JSON to disk plus gzip and brotli
// from it, stays under datasetWriteBound. Both are single measurements against
// a fixed bound with a wide margin, not a comparison of two sampled peaks.
func TestWritingTheDatasetFileHasBoundedMemory(t *testing.T) {
	if testing.Short() || underRace {
		t.Skip("writes a 90 MB dataset file")
	}
	// In a process of its own: heapPeak sets GOGC and measures the whole heap.
	if !alone(t) {
		return
	}
	in, err := testscale.Inputs(memoryShape)
	if err != nil {
		t.Fatal(err)
	}
	res, err := build.Build(in)
	if err != nil {
		t.Fatal(err)
	}
	in = nil //nolint:ineffassign,wastedassign // let the inputs go before measuring
	one := &countWriter{w: io.Discard}
	if err := build.WriteDataset(one, res.Dataset); err != nil {
		t.Fatal(err)
	}
	// Only the providers are repeated, so their share of the file sets the
	// count (the rest, pages and records, stays one copy).
	full := *res.Dataset
	full.Providers = nil
	rest := &countWriter{w: io.Discard}
	if err := build.WriteDataset(rest, &full); err != nil {
		t.Fatal(err)
	}
	for range (memoryDatasetBytes-rest.n)/(one.n-rest.n) + 1 {
		full.Providers = append(full.Providers, res.Dataset.Providers...)
	}
	sink := &countWriter{w: io.Discard}
	jsonPeak := heapPeak(func() { err = build.WriteDataset(sink, &full) })
	if err != nil {
		t.Fatal(err)
	}
	if sink.n < memoryDatasetBytes || jsonPeak > datasetJSONBound {
		t.Errorf("encoding a %d-byte dataset added %d bytes of heap (bound %d): the writer holds the file", sink.n, jsonPeak, datasetJSONBound)
	}
	files := newDatasetFiles(t.TempDir(), testCode, slog.New(slog.NewTextHandler(io.Discard, nil)))
	var f *DatasetFile
	start := time.Now()
	peak := heapPeak(func() { f, err = files.write(context.Background(), &full) })
	if err != nil {
		t.Fatal(err)
	}
	t.Logf("%d providers (%d-byte build repeated): JSON %d bytes, encoding peak heap growth %d (bound %d); file write (gzip %d, br %d) in %v, peak heap growth %d (bound %d)",
		len(full.Providers), one.n, sink.n, jsonPeak, datasetJSONBound, f.Gzip, f.Brotli, time.Since(start).Round(time.Millisecond), peak, datasetWriteBound)
	if f.Bytes != sink.n || peak > datasetWriteBound {
		t.Errorf("writing the %d-byte file (encoded %d) added %d bytes of heap, above the %d bound", f.Bytes, sink.n, peak, datasetWriteBound)
	}
}
