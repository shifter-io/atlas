package memtrace

import (
	"bytes"
	"context"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

func TestPhasesKeepTheirPeaks(t *testing.T) {
	if runtime.GOOS != "linux" {
		t.Skip("reads /proc")
	}
	var samples bytes.Buffer
	heap := filepath.Join(t.TempDir(), "heap.pprof")
	tr := Start(Options{Every: 5 * time.Millisecond, Samples: &samples, Heap: heap})
	ctx := With(context.Background(), tr)
	Mark(ctx, "small")
	Mark(context.Background(), "not traced")
	Mark(ctx, "large")
	big := make([]byte, 256<<20)
	for i := range big {
		big[i] = 1
	}
	time.Sleep(50 * time.Millisecond)
	tr.Stop()
	runtime.KeepAlive(big)
	ph := tr.Phases()
	if len(ph) != 3 || ph[0].Name != "start" || ph[1].Name != "small" || ph[2].Name != "large" {
		t.Fatalf("phases %+v", ph)
	}
	if ph[2].Peak < ph[1].Peak+200<<20 || ph[2].At < ph[1].At {
		t.Errorf("the large phase's peak %d is not 256 MiB above the small one's %d", ph[2].Peak, ph[1].Peak)
	}
	if !strings.Contains(samples.String(), "\tlarge\t") {
		t.Errorf("no sample of the large phase: %q", samples.String())
	}
	if fi, err := os.Stat(heap); err != nil || fi.Size() == 0 {
		t.Errorf("no heap profile at the peak: %v", err)
	}
}

func TestRSSAndNilTrace(t *testing.T) {
	if got := With(context.Background(), nil); got != context.Background() {
		t.Error("a nil trace changed the context")
	}
	if rss := RSS(); runtime.GOOS == "linux" && rss == 0 {
		t.Error("no RSS on Linux")
	}
}
