// Package memtrace measures where a rebuild's memory goes (SITEAPI-MEM-1):
// the process's resident memory (RSS) sampled at a fixed interval, the peak
// of each phase a rebuild marks (Mark), and, when asked, every sample and a
// heap profile taken at the peak.
package memtrace

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"os"
	"runtime/pprof"
	"strconv"
	"strings"
	"sync"
	"time"
)

// Phase is one marked phase: its name, when it started (since Start), the
// RSS then, and the highest RSS sampled until the next phase started.
type Phase struct {
	Name           string
	At             time.Duration
	StartRSS, Peak uint64
}

// Options configure a Trace. Every is the sampling interval (default 200 ms);
// Samples, when set, gets one line per sample (milliseconds since Start,
// phase, RSS bytes); Heap, when set, is the file a heap profile is written to
// at every new peak at least 64 MiB above the last profiled one; Log, when
// set, logs each phase's start with the previous phase's peak.
type Options struct {
	Every   time.Duration
	Samples io.Writer
	Heap    string
	Log     *slog.Logger
}

// heapStep is how far above the last profiled peak a new peak is profiled.
const heapStep = 64 << 20

// Trace samples the RSS from Start until Stop.
type Trace struct {
	o              Options
	start          time.Time
	mu             sync.Mutex
	phases         []Phase
	peak, profiled uint64
	stop, done     chan struct{}
}

// Start samples every o.Every in a goroutine of its own, in a phase "start".
func Start(o Options) *Trace {
	if o.Every <= 0 {
		o.Every = 200 * time.Millisecond
	}
	rss := RSS()
	t := &Trace{o: o, start: time.Now(), phases: []Phase{{Name: "start", StartRSS: rss, Peak: rss}},
		stop: make(chan struct{}), done: make(chan struct{})}
	go t.run()
	return t
}

func (t *Trace) run() {
	defer close(t.done)
	tick := time.NewTicker(t.o.Every)
	defer tick.Stop()
	for {
		select {
		case <-t.stop:
			return
		case <-tick.C:
			t.sample()
		}
	}
}

// sample records the RSS now in the current phase.
func (t *Trace) sample() {
	rss := RSS()
	t.mu.Lock()
	p := &t.phases[len(t.phases)-1]
	p.Peak = max(p.Peak, rss)
	name, at := p.Name, time.Since(t.start)
	profile := t.o.Heap != "" && rss > t.peak && rss >= t.profiled+heapStep
	t.peak = max(t.peak, rss)
	if profile {
		t.profiled = rss
	}
	if t.o.Samples != nil {
		fmt.Fprintf(t.o.Samples, "%d\t%s\t%d\n", at.Milliseconds(), name, rss)
	}
	t.mu.Unlock()
	if profile {
		t.writeHeap()
	}
}

// writeHeap writes the heap profile to o.Heap; a failure is logged.
func (t *Trace) writeHeap() {
	f, err := os.Create(t.o.Heap)
	if err == nil {
		err = pprof.Lookup("heap").WriteTo(f, 0)
		if cerr := f.Close(); err == nil {
			err = cerr
		}
	}
	if err != nil && t.o.Log != nil {
		t.o.Log.Warn("memory trace: heap profile", "error", err)
	}
}

// Mark starts phase name, sampling the RSS at its start.
func (t *Trace) Mark(name string) {
	t.sample()
	rss := RSS()
	t.mu.Lock()
	prev := t.phases[len(t.phases)-1]
	t.phases = append(t.phases, Phase{Name: name, At: time.Since(t.start), StartRSS: rss, Peak: rss})
	t.mu.Unlock()
	if t.o.Log != nil {
		t.o.Log.Info("memory phase", "phase", name, "rss_mb", rss>>20, "previous", prev.Name, "previous_peak_mb", prev.Peak>>20)
	}
}

// Phases are the phases so far, in order.
func (t *Trace) Phases() []Phase {
	t.mu.Lock()
	defer t.mu.Unlock()
	return append([]Phase(nil), t.phases...)
}

// Stop takes a last sample and stops sampling.
func (t *Trace) Stop() {
	close(t.stop)
	<-t.done
	t.sample()
}

// RSS is the process's resident memory in bytes (Linux), 0 elsewhere.
func RSS() uint64 {
	b, err := os.ReadFile("/proc/self/statm")
	if err != nil {
		return 0
	}
	f := strings.Fields(string(b))
	if len(f) < 2 {
		return 0
	}
	pages, _ := strconv.ParseUint(f[1], 10, 64)
	return pages * uint64(os.Getpagesize())
}

type key struct{}

// With is ctx whose Mark calls mark t (nil: none).
func With(ctx context.Context, t *Trace) context.Context {
	if t == nil {
		return ctx
	}
	return context.WithValue(ctx, key{}, t)
}

// Mark starts phase name in ctx's Trace, if any.
func Mark(ctx context.Context, name string) {
	if t, _ := ctx.Value(key{}).(*Trace); t != nil {
		t.Mark(name)
	}
}
