package server

import (
	"bytes"
	"io"
	"runtime"
	"testing"

	"github.com/andybalholm/brotli"
)

// The brotli window holds the body, no more (SITEAPI-MEM-1): the match
// finder's memory follows the window.
func TestBrotliWindowFitsTheBody(t *testing.T) {
	for _, c := range []struct{ n, max, want int }{{0, 22, 10}, {1000, 22, 10}, {1007, 22, 10}, {1008, 22, 11}, {20 << 10, 22, 15},
		{4 << 20, 22, 22}, {1900 << 10, 24, 21}, {64 << 20, 24, 24}} {
		if got := fitWindow(c.n, c.max); got != c.want {
			t.Errorf("fitWindow(%d, %d) = %d, want %d", c.n, c.max, got, c.want)
		}
		if got := fitWindow(c.n, c.max); c.n > 0 && 1<<got-16 < min(c.n, 1<<c.max-16) {
			t.Errorf("fitWindow(%d, %d) = %d does not hold the body", c.n, c.max, got)
		}
	}
	body := bytes.Repeat([]byte(`{"name":"a network","ips":12345},`), 3000)
	for _, level := range []Level{LevelBest, LevelFast} {
		_, br := compress(body, level)
		dec, err := io.ReadAll(brotli.NewReader(bytes.NewReader(br)))
		if err != nil || !bytes.Equal(dec, body) {
			t.Errorf("level %d: the brotli body does not decode to the body (%v)", level, err)
		}
	}
}

func TestRenderWorkersAreBounded(t *testing.T) {
	if got := RenderWorkers(); got < 1 || got > maxRenderWorkers || got > runtime.GOMAXPROCS(0) {
		t.Errorf("RenderWorkers() = %d", got)
	}
}
