package server

import (
	"context"
	"io"
	"log/slog"
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

// The dataset file's bounds at run A (DATASET-SIZE-2): its size, and the time
// to write it with its .gz and .br.
const (
	runAMaxBytes = 15_000_000
	runAMaxWrite = 2 * time.Second
)

// The run A file, every list as its first page, total and page count, is at
// most runAMaxBytes and written within runAMaxWrite (DATASET-SIZE-2); with
// every list row it was about 0.5 GB and took minutes.
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
	start := time.Now()
	f, err := files.write(context.Background(), res.Dataset)
	took := time.Since(start)
	if err != nil {
		t.Fatal(err)
	}
	t.Logf("run A file: %d bytes, gzip %d, br %d, written in %v", f.Bytes, f.Gzip, f.Brotli, took.Round(time.Millisecond))
	if f.Bytes > runAMaxBytes || took > runAMaxWrite {
		t.Errorf("the run A file is %d bytes, written in %v: above %d bytes or %v", f.Bytes, took, runAMaxBytes, runAMaxWrite)
	}
}
