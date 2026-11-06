package server

// The dataset file's writer: the JSON streamed to a .tmp file, its .gz and
// .br compressed from it, the meta file, each fsynced and renamed into place.

import (
	"compress/gzip"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sync"

	"github.com/Zeus-Development/proxy-index/api/internal/build"
	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/andybalholm/brotli"
)

// datasetBrotli is the dataset file's brotli setting: quality 5 with a 4 MB
// window. Its encoder state is about 110 MB whatever the file's size; the
// 16 MB window the in-memory dataset used needs about 330 MB and gave no
// smaller file on the QA-1 dataset. Quality 10 would take minutes of CPU
// every hour at the 0.5 GB a v4 file reached with every list row (contract
// K11, before DATASET-SIZE-2).
var datasetBrotli = brotli.WriterOptions{Quality: 5, LGWin: 22}

// datasetGzipLevel is the dataset file's gzip level, for the same reason.
const datasetGzipLevel = 6

// write streams ds to a new dataset file and its codings and renames them
// into place.
func (d *datasetFiles) write(ds *model.Dataset) (*DatasetFile, error) {
	tmp, sum, n, err := d.writeJSON(ds)
	if err != nil {
		return nil, err
	}
	defer os.Remove(tmp) //nolint:errcheck // gone after the rename
	f := &DatasetFile{SHA256: sum, Bytes: n, dir: d.dir}
	var gz, br string
	var gzErr, brErr error
	var wg sync.WaitGroup
	wg.Go(func() {
		gz, f.Gzip, gzErr = d.compress(tmp, func(w io.Writer) io.WriteCloser {
			zw, _ := gzip.NewWriterLevel(w, datasetGzipLevel)
			return zw
		})
	})
	wg.Go(func() {
		br, f.Brotli, brErr = d.compress(tmp, func(w io.Writer) io.WriteCloser { return brotli.NewWriterOptions(w, datasetBrotli) })
	})
	wg.Wait()
	defer os.Remove(gz) //nolint:errcheck
	defer os.Remove(br) //nolint:errcheck
	if err := errors.Join(gzErr, brErr); err != nil {
		return nil, err
	}
	for from, to := range map[string]string{tmp: f.Name(), gz: f.Name() + ".gz", br: f.Name() + ".br"} {
		if err := os.Rename(from, filepath.Join(d.dir, to)); err != nil {
			return nil, err
		}
	}
	return f, syncDir(d.dir)
}

// writeJSON writes the encoded dataset to a .tmp file and returns its path,
// SHA-256 and size.
func (d *datasetFiles) writeJSON(ds *model.Dataset) (path, sum string, n int64, err error) {
	out, err := createTemp(d.dir, "dataset-*.json.tmp")
	if err != nil {
		return "", "", 0, err
	}
	h := sha256.New()
	cw := &countWriter{w: io.MultiWriter(out, h)}
	err = build.WriteDataset(cw, ds)
	if err == nil {
		err = out.Sync()
	}
	if cerr := out.Close(); err == nil {
		err = cerr
	}
	if err != nil {
		os.Remove(out.Name()) //nolint:errcheck
		return "", "", 0, err
	}
	return out.Name(), hex.EncodeToString(h.Sum(nil)), cw.n, nil
}

// compress writes the coding of the file at src to a fsynced .tmp file.
func (d *datasetFiles) compress(src string, coder func(io.Writer) io.WriteCloser) (path string, n int64, err error) {
	in, err := os.Open(src)
	if err != nil {
		return "", 0, err
	}
	defer in.Close() //nolint:errcheck // read only
	out, err := createTemp(d.dir, "dataset-*.tmp")
	if err != nil {
		return "", 0, err
	}
	cw := &countWriter{w: out}
	zw := coder(cw)
	_, err = io.Copy(zw, in)
	if cerr := zw.Close(); err == nil {
		err = cerr
	}
	if err == nil {
		err = out.Sync()
	}
	if cerr := out.Close(); err == nil {
		err = cerr
	}
	return out.Name(), cw.n, err
}

// saveMeta writes the meta file as a .tmp, fsyncs and renames it.
func (d *datasetFiles) saveMeta(meta datasetMeta) error {
	raw, err := json.Marshal(meta)
	if err != nil {
		return err
	}
	out, err := createTemp(d.dir, "dataset-meta-*.tmp")
	if err != nil {
		return err
	}
	_, err = out.Write(raw)
	if err == nil {
		err = out.Sync()
	}
	if cerr := out.Close(); err == nil {
		err = cerr
	}
	if err == nil {
		err = os.Rename(out.Name(), filepath.Join(d.dir, datasetMetaName))
	}
	if err != nil {
		os.Remove(out.Name()) //nolint:errcheck
		return fmt.Errorf("dataset meta: %w", err)
	}
	return syncDir(d.dir)
}

// createTemp creates a .tmp file readable by all (0644, like the files a web
// server or backup job may read beside siteapi), not CreateTemp's 0600.
func createTemp(dir, pattern string) (*os.File, error) {
	f, err := os.CreateTemp(dir, pattern)
	if err == nil {
		if err = f.Chmod(0o644); err != nil {
			f.Close()           //nolint:errcheck
			os.Remove(f.Name()) //nolint:errcheck
		}
	}
	return f, err
}

// syncDir fsyncs a directory, so the renames in it survive a crash.
func syncDir(dir string) error {
	f, err := os.Open(dir)
	if err != nil {
		return err
	}
	err = f.Sync()
	if cerr := f.Close(); err == nil {
		err = cerr
	}
	return err
}

type countWriter struct {
	w io.Writer
	n int64
}

func (c *countWriter) Write(p []byte) (int, error) {
	n, err := c.w.Write(p)
	c.n += int64(n)
	return n, err
}
