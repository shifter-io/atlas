package server

// The dataset file (contract K4). /v1/dataset redirects to
// /v1/files/dataset-<sha256>.json, a file in Options.FilesDir with its .gz and
// .br beside it. It is written only when the marker (datasetMarker: the
// published build's created_at and the site tables' last change) differs from
// the one it was written for, never on test events, and it is streamed: the
// JSON one provider at a time (build.WriteDataset), then compressed from the
// file on disk, so it is never held whole in memory. Every file is written as
// a .tmp, fsynced and renamed. The file it replaces is still served under its
// own name for datasetGrace after the replacement, then deleted. The meta file
// (dataset.meta.json) records the marker and the files, so a restart reuses a
// file whose marker matches instead of writing it again.

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/Zeus-Development/proxy-index/api/internal/build"
	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/Zeus-Development/proxy-index/api/internal/store"
)

// datasetGrace is how long a replaced dataset file is still served.
const datasetGrace = 10 * time.Minute

// datasetDownloadName is the file name a browser saves the dataset file as.
// Content-Disposition: attachment makes a cross-origin link download it
// (the HTML download attribute is ignored there) instead of opening the
// JSON in a tab.
const datasetDownloadName = "shifter-proxy-index-dataset.json"

// datasetMetaName is the meta file in the files directory.
const datasetMetaName = "dataset.meta.json"

// ownFile is every name the files directory may hold that this code wrote:
// dataset files, their codings and unfinished .tmp files.
var ownFile = regexp.MustCompile(`^(dataset-[0-9a-f]{64}\.json(\.gz|\.br)?|dataset-.*\.tmp)$`)

// DatasetFile is one dataset file on disk with its .gz and .br.
type DatasetFile struct {
	SHA256 string `json:"sha256"`
	Bytes  int64  `json:"bytes"`
	Gzip   int64  `json:"gzip"`
	Brotli int64  `json:"br"`
	// Until is when a replaced file stops being served (zero while current).
	Until time.Time `json:"until,omitzero"`
	dir   string
}

// Name is the file's name: dataset-<sha256>.json.
func (f *DatasetFile) Name() string { return "dataset-" + f.SHA256 + ".json" }

// Path is the file's URL path.
func (f *DatasetFile) Path() string { return "/v1/files/" + f.Name() }

// variant is a coding's file, Content-Encoding, length and ETag (one strong
// ETag per coding, as Rendered.Variant).
func (f *DatasetFile) variant(e Encoding) (file, coding string, length int64, etag string) {
	switch e {
	case Brotli:
		return f.Name() + ".br", "br", f.Brotli, `"` + f.SHA256 + `-br"`
	case Gzip:
		return f.Name() + ".gz", "gzip", f.Gzip, `"` + f.SHA256 + `-gz"`
	}
	return f.Name(), "", f.Bytes, `"` + f.SHA256 + `"`
}

// datasetMarker is what the dataset file is written for (K4): the published
// build's created_at and the site tables' newest updated_at, plus the site
// revision counter, which their statement triggers bump on every write, so a
// deleted catalog row (which changes no updated_at) also writes a new file,
// and the framework's product count and targets digest, which the dataset's
// targets and providers' products are built from. The file is written again
// whenever the marker differs, which includes the marker being newer.
func datasetMarker(fp store.Fingerprint) string {
	b, _ := json.Marshal(struct {
		Build    *time.Time `json:"build_created_at"`
		Site     *time.Time `json:"site_updated_at"`
		Revision int64      `json:"site_revision"`
		Products int64      `json:"framework_products"`
		Targets  *string    `json:"framework_targets_digest"`
	}{fp.BuildCreatedAt, fp.SiteUpdatedAt, fp.SiteRevision, fp.Products, fp.TargetsDigest})
	return string(b)
}

// CodeID identifies the code that encodes the dataset file: the binary's git
// revision, or "" when it is unknown or the tree was modified.
func CodeID(b Binary) string {
	if b.Revision == nil || b.Modified == nil || *b.Modified {
		return ""
	}
	return *b.Revision
}

// datasetMeta is the meta file: the marker the current file was written for,
// the code that wrote it (CodeID), the current file and the replaced ones
// still in their grace.
type datasetMeta struct {
	Marker  string         `json:"marker"`
	Code    string         `json:"code"`
	Current *DatasetFile   `json:"current"`
	Retired []*DatasetFile `json:"retired"`
}

// datasetFiles owns the files directory. ensure and expire run in the
// refresh (one at a time); lookup runs per request. code is this process's
// CodeID.
type datasetFiles struct {
	dir    string
	code   string
	log    *slog.Logger
	mu     sync.Mutex
	meta   datasetMeta
	loaded bool
}

func newDatasetFiles(dir, code string, log *slog.Logger) *datasetFiles {
	return &datasetFiles{dir: dir, code: code, log: log}
}

// ensure returns the dataset file for marker, writing it from ds unless the
// current file was written for that marker. The first call reads the meta
// file an earlier process left, so a restart reuses its file. A write that
// ctx stops leaves the current file and the meta as they were.
func (d *datasetFiles) ensure(ctx context.Context, ds *model.Dataset, marker string, now time.Time) (*DatasetFile, error) {
	if !d.loaded {
		d.load(now)
	}
	d.mu.Lock()
	meta := d.meta
	d.mu.Unlock()
	if meta.Current != nil && meta.Marker == marker {
		return meta.Current, nil
	}
	f, err := d.write(ctx, ds)
	if err != nil {
		return nil, fmt.Errorf("dataset file: %w", err)
	}
	next := datasetMeta{Marker: marker, Code: d.code, Current: f}
	for _, r := range append(append([]*DatasetFile{}, meta.Retired...), meta.Current) {
		if r != nil && r.SHA256 != f.SHA256 && (r.Until.IsZero() || r.Until.After(now)) {
			old := *r
			if old.Until.IsZero() {
				old.Until = now.Add(datasetGrace)
			}
			next.Retired = append(next.Retired, &old)
		}
	}
	if err := d.saveMeta(next); err != nil {
		return nil, err
	}
	d.mu.Lock()
	d.meta = next
	d.mu.Unlock()
	d.prune()
	d.log.Info("dataset file written", "file", f.Name(), "bytes", f.Bytes, "gzip", f.Gzip, "br", f.Brotli)
	return f, nil
}

// lookup returns the current file or a replaced one still in its grace.
func (d *datasetFiles) lookup(name string, now time.Time) *DatasetFile {
	d.mu.Lock()
	defer d.mu.Unlock()
	if c := d.meta.Current; c != nil && c.Name() == name {
		return c
	}
	for _, r := range d.meta.Retired {
		if r.Name() == name && r.Until.After(now) {
			return r
		}
	}
	return nil
}

// expire drops the replaced files whose grace ended (every refresh tick).
func (d *datasetFiles) expire(now time.Time) {
	d.mu.Lock()
	keep := d.meta.Retired[:0:0]
	for _, r := range d.meta.Retired {
		if r.Until.After(now) {
			keep = append(keep, r)
		}
	}
	changed := len(keep) != len(d.meta.Retired)
	next := datasetMeta{Marker: d.meta.Marker, Code: d.meta.Code, Current: d.meta.Current, Retired: keep}
	d.mu.Unlock()
	if !changed {
		return
	}
	if err := d.saveMeta(next); err != nil {
		d.log.Warn("dataset meta not saved", "error", err)
		return
	}
	d.mu.Lock()
	d.meta = next
	d.mu.Unlock()
	d.prune()
}

// load reads the meta file. A file it names that is missing or of another
// size is not used; replaced files past their grace are dropped. A current
// file written by other code (or when either code is unknown) is not reused:
// it is served for datasetGrace like a replaced file, and the next ensure
// writes the file again.
func (d *datasetFiles) load(now time.Time) {
	d.loaded = true
	raw, err := os.ReadFile(filepath.Join(d.dir, datasetMetaName))
	var meta datasetMeta
	if err == nil {
		err = json.Unmarshal(raw, &meta)
	}
	if err != nil {
		if !errors.Is(err, os.ErrNotExist) {
			d.log.Warn("dataset meta unreadable; writing the file again", "error", err)
		}
		meta = datasetMeta{}
	}
	if meta.Current != nil && !d.intact(meta.Current) {
		meta.Current, meta.Marker = nil, ""
	}
	if meta.Current != nil && (d.code == "" || meta.Code != d.code) {
		d.log.Info("dataset file written by other code; writing it again", "file", meta.Current.Name(), "code", meta.Code)
		meta.Current.Until = now.Add(datasetGrace)
		meta.Retired = append(meta.Retired, meta.Current)
		meta.Current, meta.Marker = nil, ""
	}
	retired := []*DatasetFile{}
	for _, r := range meta.Retired {
		if r != nil && r.Until.After(now) && d.intact(r) {
			retired = append(retired, r)
		}
	}
	meta.Retired = retired
	d.mu.Lock()
	d.meta = meta
	d.mu.Unlock()
	d.prune()
}

// intact sets f's directory and reports whether its three files have the
// recorded sizes.
func (d *datasetFiles) intact(f *DatasetFile) bool {
	f.dir = d.dir
	for _, e := range []Encoding{Identity, Gzip, Brotli} {
		name, _, size, _ := f.variant(e)
		st, err := os.Stat(filepath.Join(d.dir, name))
		if err != nil || st.Size() != size {
			return false
		}
	}
	return true
}

// prune deletes this code's files that the meta does not name: replaced
// files past their grace and .tmp files left by an interrupted write.
func (d *datasetFiles) prune() {
	d.mu.Lock()
	keep := map[string]bool{}
	for _, f := range append([]*DatasetFile{d.meta.Current}, d.meta.Retired...) {
		if f != nil {
			keep[f.Name()], keep[f.Name()+".gz"], keep[f.Name()+".br"] = true, true, true
		}
	}
	d.mu.Unlock()
	entries, err := os.ReadDir(d.dir)
	if err != nil {
		d.log.Warn("files directory unreadable", "error", err)
		return
	}
	for _, e := range entries {
		if n := e.Name(); ownFile.MatchString(n) && !keep[n] {
			if err := os.Remove(filepath.Join(d.dir, n)); err != nil {
				d.log.Warn("old dataset file not removed", "file", n, "error", err)
			}
		}
	}
}

// serveDatasetFile sends a dataset file in the negotiated coding, as write
// sends a rendered body: the same ETags, If-None-Match handling and headers,
// plus Content-Disposition, which a 304 also carries so a revalidating CDN
// updates its stored copy.
func (s *Server) serveDatasetFile(w http.ResponseWriter, r *http.Request, v *Version, f *DatasetFile) {
	name, coding, length, etag := f.variant(Negotiate(r.Header.Get("Accept-Encoding")))
	h := w.Header()
	h.Set("Vary", "Accept-Encoding")
	h.Set("ETag", etag)
	h.Set("Cache-Control", CacheFile)
	h.Set("X-Content-Type-Options", "nosniff")
	h.Set("Content-Disposition", `attachment; filename="`+datasetDownloadName+`"`)
	h.Set("Access-Control-Allow-Origin", "*")
	h.Set("X-Site-Version", strconv.FormatUint(v.ID, 10))
	if (&Rendered{ETag: `"` + f.SHA256 + `"`}).Matches(r.Header.Get("If-None-Match")) {
		w.WriteHeader(http.StatusNotModified)
		return
	}
	file, err := os.Open(filepath.Join(f.dir, name))
	if err != nil {
		h.Del("ETag")
		h.Del("Content-Disposition")
		s.log.Error("dataset file unreadable", "file", name, "error", err)
		writeError(w, http.StatusServiceUnavailable, build.Unavailable(build.ReasonDatasetFile))
		return
	}
	defer file.Close() //nolint:errcheck // read only
	h.Set("Content-Type", "application/json; charset=utf-8")
	if coding != "" {
		h.Set("Content-Encoding", coding)
	}
	h.Set("Content-Length", strconv.FormatInt(length, 10))
	w.WriteHeader(http.StatusOK)
	if r.Method != http.MethodHead {
		io.Copy(w, file) //nolint:errcheck // the client went away
	}
}

// serveDataset redirects to the announced dataset file (the previous build's
// while a CDN purge is pending, announce). The Location is
// relative (files/dataset-<sha>.json), so it resolves under any path prefix
// a proxy adds (the site's /api/site/v1/dataset).
func (s *Server) serveDataset(w http.ResponseWriter, r *http.Request) {
	v := s.announced()
	if v == nil || v.Dataset == nil {
		s.unavailable(w)
		return
	}
	h := w.Header()
	h.Set("Location", strings.TrimPrefix(v.DatasetPath, "/v1/"))
	h.Set("Cache-Control", CacheVersion)
	h.Set("Access-Control-Allow-Origin", "*")
	h.Set("X-Site-Version", strconv.FormatUint(v.ID, 10))
	w.WriteHeader(http.StatusFound)
}

// datasetName is a dataset file's name: dataset-<hex sha256>.json.
var datasetName = regexp.MustCompile(`^dataset-[0-9a-f]{64}\.json$`)

// serveFile serves the current dataset file and, for datasetGrace after it
// was replaced, the one before. Any other dataset name (an older revision) is
// redirected, uncached, to the current file, so a download or a build never
// gets a 404 because the data changed under it; other names are 404.
func (s *Server) serveFile(w http.ResponseWriter, r *http.Request) {
	v := s.current.Load()
	if v == nil || v.Dataset == nil {
		s.unavailable(w)
		return
	}
	name := r.PathValue("name")
	switch f := s.datasetFile(v, name); {
	case f != nil:
		s.serveDatasetFile(w, r, v, f)
	case datasetName.MatchString(name):
		h := w.Header()
		h.Set("Location", strings.TrimPrefix(v.DatasetPath, "/v1/files/"))
		h.Set("Cache-Control", CacheNone)
		h.Set("Access-Control-Allow-Origin", "*")
		w.WriteHeader(http.StatusFound)
	default:
		notFound(w, r)
	}
}

// datasetFile is v's dataset file or a replaced one still in its grace, by name.
func (s *Server) datasetFile(v *Version, name string) *DatasetFile {
	if name == v.Dataset.Name() {
		return v.Dataset
	}
	if s.files == nil {
		return nil
	}
	return s.files.lookup(name, s.opts.Now())
}
