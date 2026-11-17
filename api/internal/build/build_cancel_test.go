package build

import (
	"context"
	"errors"
	"sync/atomic"
	"testing"

	"github.com/shifter-io/atlas/api/internal/testscale"
)

// stopAfter is a context that ends (Err is context.Canceled) after its first
// n checks: a shutdown that arrives at any point of a build.
type stopAfter struct {
	context.Context
	n atomic.Int64
}

func (c *stopAfter) Err() error {
	if c.n.Add(-1) < 0 {
		return context.Canceled
	}
	return nil
}

// A shutdown stops a build at its next step or network page, whichever check
// it reaches first: the build returns context.Canceled and no result.
func TestBuildContextStopsWhenItsContextEnds(t *testing.T) {
	t.Parallel()
	in, err := testscale.Inputs(testscale.Shape{Networks: 3, Countries: 2, Exits: 3, Tail: 5})
	if err != nil {
		t.Fatal(err)
	}
	// 9 steps, then one check per network before the pages' rows are read.
	for n := range int64(12) {
		ctx := &stopAfter{Context: context.Background()}
		ctx.n.Store(n)
		if res, err := BuildContext(ctx, in); !errors.Is(err, context.Canceled) || res != nil {
			t.Fatalf("stopped after %d checks: result %v, error %v; want context.Canceled", n, res != nil, err)
		}
	}
	if _, err := BuildContext(context.Background(), in); err != nil {
		t.Fatal(err)
	}
}
