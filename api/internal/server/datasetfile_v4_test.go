package server

import (
	"compress/gzip"
	"context"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"runtime"
	"testing"
	"time"

	"github.com/Zeus-Development/atlas/api/internal/build"
	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/testscale"
	"github.com/andybalholm/brotli"
)

// memoryListBytes is the least the v4 stand-in's lists add to the file.
const memoryListBytes = 90_000_000

// The file is compressed at gzip level 6 (its header's XFL byte is 0; level
// 9 writes 2) and brotli quality 5 with a 4 MB window: quality 10 and gzip 9
// would take minutes of CPU every hour at the 0.5 GB a v4 file reaches.
func TestTheDatasetFileCompressesAtGzip6AndBrotli5(t *testing.T) {
	t.Parallel()
	s := newTestServer(t, &fakeSource{rev: 1, events: events(5), complete: true})
	f := s.Current().Dataset
	gz, err := os.ReadFile(filepath.Join(f.dir, f.Name()+".gz"))
	if err != nil || len(gz) < 10 || gz[8] != 0 {
		t.Errorf("gzip header %x (%v): not level 6", gz[:min(len(gz), 10)], err)
	}
	if datasetGzipLevel != 6 || datasetBrotli != (brotli.WriterOptions{Quality: 5, LGWin: 22}) {
		t.Errorf("gzip %d, brotli %+v", datasetGzipLevel, datasetBrotli)
	}
}

// Writing a v4 file whose lists hold at least 90 MB of rows (QA-1's v4
// build, its lists repeated as more networks) adds at most datasetJSONBound
// of heap while encoding and datasetWriteBound for the whole write, as for a
// file of providers (regression case 4 of API-DATASET-2). It logs the write
// and each coding's seconds per MB.
func TestWritingAV4DatasetFileHasBoundedMemory(t *testing.T) {
	if testing.Short() || underRace {
		t.Skip("writes a 90 MB dataset file")
	}
	// In a process of its own: heapPeak sets GOGC and measures the whole heap.
	if !alone(t) {
		return
	}
	in, err := testscale.NetworkInputs(testscale.QA1Networks)
	if err != nil {
		t.Fatal(err)
	}
	res, err := build.Build(in)
	if err != nil {
		t.Fatal(err)
	}
	in = nil //nolint:ineffassign,wastedassign // let the inputs go before measuring
	lists := res.Dataset.Lists.(*build.DatasetLists)
	full := *res.Dataset
	full.Lists = &build.DatasetLists{}
	rest := &countWriter{w: io.Discard}
	if err := build.WriteDataset(rest, &full); err != nil {
		t.Fatal(err)
	}
	full.Lists = lists
	one := &countWriter{w: io.Discard}
	if err := build.WriteDataset(one, &full); err != nil {
		t.Fatal(err)
	}
	files := newDatasetFiles(t.TempDir(), testCode, slog.New(slog.NewTextHandler(io.Discard, nil)))
	if f, err := files.write(context.Background(), &full); err == nil {
		t.Logf("QA-1 v4 file: %d bytes (lists %d), gzip %d, br %d", f.Bytes, one.n-rest.n, f.Gzip, f.Brotli)
	} else {
		t.Fatal(err)
	}
	full.Lists = lists.Repeated(int(memoryListBytes/(one.n-rest.n) + 1))
	sink := &countWriter{w: io.Discard}
	jsonPeak := heapPeak(func() { err = build.WriteDataset(sink, &full) })
	if err != nil {
		t.Fatal(err)
	}
	if sink.n-rest.n < memoryListBytes || jsonPeak > datasetJSONBound {
		t.Errorf("encoding %d bytes of lists added %d bytes of heap (bound %d): the writer holds them", sink.n-rest.n, jsonPeak, datasetJSONBound)
	}
	var f *DatasetFile
	start := time.Now()
	peak := heapPeak(func() { f, err = files.write(context.Background(), &full) })
	if err != nil {
		t.Fatal(err)
	}
	whole := time.Since(start)
	if f.Bytes != sink.n || peak > datasetWriteBound {
		t.Errorf("writing the %d-byte file (encoded %d) added %d bytes of heap, above the %d bound", f.Bytes, sink.n, peak, datasetWriteBound)
	}
	t.Logf("v4 file %d bytes (lists %d per copy), gzip %d, br %d; encoding peak heap growth %d (bound %d); whole write %v, peak %d (bound %d)",
		f.Bytes, one.n-rest.n, f.Gzip, f.Brotli, jsonPeak, datasetJSONBound, whole.Round(time.Millisecond), peak, datasetWriteBound)
	logTimes(t, files, &full, f)
	var m runtime.MemStats
	runtime.ReadMemStats(&m)
	t.Logf("process memory from the OS: %.0f MB (heap in use %.0f MB)", float64(m.Sys)/1e6, float64(m.HeapInuse)/1e6)
}

// logTimes times the JSON write and each coding alone on the written file,
// in seconds per MB of JSON.
func logTimes(t *testing.T, files *datasetFiles, ds *model.Dataset, f *DatasetFile) {
	t.Helper()
	mb := float64(f.Bytes) / 1e6
	src := filepath.Join(f.dir, f.Name())
	start := time.Now()
	tmp, _, _, err := files.writeJSON(context.Background(), ds)
	if err != nil {
		t.Fatal(err)
	}
	identity := time.Since(start)
	os.Remove(tmp) //nolint:errcheck
	times := map[string]time.Duration{}
	for name, coder := range map[string]func(io.Writer) io.WriteCloser{
		"gzip 6": func(w io.Writer) io.WriteCloser {
			zw, _ := gzip.NewWriterLevel(w, datasetGzipLevel)
			return zw
		},
		"brotli 5": func(w io.Writer) io.WriteCloser { return brotli.NewWriterOptions(w, datasetBrotli) },
	} {
		start := time.Now()
		path, _, err := files.compress(context.Background(), src, coder)
		if err != nil {
			t.Fatal(err)
		}
		times[name] = time.Since(start)
		os.Remove(path) //nolint:errcheck
	}
	t.Logf("seconds per MB of JSON (%.1f MB): identity %.4f, gzip 6 %.4f, brotli 5 %.4f", mb, identity.Seconds()/mb,
		times["gzip 6"].Seconds()/mb, times["brotli 5"].Seconds()/mb)
}
