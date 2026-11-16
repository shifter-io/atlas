package server

import (
	"context"
	"io"
	"log/slog"
	"syscall"
	"testing"
	"time"

	"github.com/Zeus-Development/atlas/api/internal/build"
	"github.com/Zeus-Development/atlas/api/internal/testscale"
)

// runA is the shape of SUM-MEM-1's run A (scripts/scale/generate.py --scale
// 0.0625: 18 networks x 249 countries) as a v4 build: each network one
// summary over 249 requested countries, with network lists of 249 exit
// countries, the tails and 2,000 City x ASN rows per window. Longer lists
// would change only the lists' totals: the file holds their first pages.
var runA = testscale.NetworkShape{Shape: testscale.Shape{Networks: 18, Countries: 1, Exits: 249, Tail: 200, Unmeasured: 3, Events: 559,
	Browser: true, CityAsn: 2000}, Requested: 249}

// The dataset file's bounds at run A (DATASET-SIZE-2): its size, and the
// processor time to write it with its .gz and .br.
const (
	runAMaxBytes = 15_000_000
	runAMaxWrite = 2 * time.Second
)

// The run A file, every list as its first page, total and page count, is at
// most runAMaxBytes and written within runAMaxWrite of processor time
// (DATASET-SIZE-2); with every list row it was about 0.5 GB and took
// minutes. Processor time, not elapsed time: the write's own work, which a
// runner busy with other jobs does not stretch (390 ms to 620 ms elapsed on
// the same commit). Serial, so no other test of the package runs beside it.
func TestTheRunADatasetFileIsSmallAndQuickToWrite(t *testing.T) {
	if testing.Short() || underRace {
		t.Skip("builds 18 networks x 249 countries")
	}
	in, err := testscale.NetworkInputs(runA)
	if err != nil {
		t.Fatal(err)
	}
	res, err := build.Build(in)
	if err != nil {
		t.Fatal(err)
	}
	files := newDatasetFiles(t.TempDir(), testCode, slog.New(slog.NewTextHandler(io.Discard, nil)))
	start, cpuStart := time.Now(), processorTime(t)
	f, err := files.write(context.Background(), res.Dataset)
	took, cpu := time.Since(start), processorTime(t)-cpuStart
	if err != nil {
		t.Fatal(err)
	}
	t.Logf("run A file: %d bytes, gzip %d, br %d, written in %v (%v of processor time)", f.Bytes, f.Gzip, f.Brotli,
		took.Round(time.Millisecond), cpu.Round(time.Millisecond))
	if f.Bytes > runAMaxBytes || cpu > runAMaxWrite {
		t.Errorf("the run A file is %d bytes, written in %v of processor time: above %d bytes or %v", f.Bytes, cpu, runAMaxBytes, runAMaxWrite)
	}
}

// processorTime is the user and system time the test process has used.
func processorTime(t *testing.T) time.Duration {
	t.Helper()
	var u syscall.Rusage
	if err := syscall.Getrusage(syscall.RUSAGE_SELF, &u); err != nil {
		t.Fatal(err)
	}
	return time.Duration(u.Utime.Nano() + u.Stime.Nano())
}
