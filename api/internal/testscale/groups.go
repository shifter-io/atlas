package testscale

import (
	"encoding/json"
	"runtime"
	"sync"
	"sync/atomic"
)

// each calls fn for every index below n, on as many goroutines as there are
// CPUs, and returns the error of the lowest index that failed. Each call
// writes only its own index's results, so the outcome is the same as one
// loop's: a QA-1-sized build is 720 product groups, each encoded alone.
func each(n int, fn func(i int) error) error {
	errs := make([]error, n)
	var next atomic.Int64
	var wg sync.WaitGroup
	for range min(n, runtime.GOMAXPROCS(0)) {
		wg.Go(func() {
			for i := int(next.Add(1) - 1); i < n; i = int(next.Add(1) - 1) {
				errs[i] = fn(i)
			}
		})
	}
	wg.Wait()
	for _, err := range errs {
		if err != nil {
			return err
		}
	}
	return nil
}

// eachGroup decodes the payload's top level and each product group alone
// (never the whole payload at once), applies edit to the group and returns
// the top level and the groups re-encoded, in order.
func eachGroup(payload []byte, edit func(map[string]any)) (map[string]json.RawMessage, [][]byte, error) {
	var doc map[string]json.RawMessage
	if err := json.Unmarshal(payload, &doc); err != nil {
		return nil, nil, err
	}
	var raw []json.RawMessage
	if err := json.Unmarshal(doc["products"], &raw); err != nil {
		return nil, nil, err
	}
	groups := make([][]byte, len(raw))
	err := each(len(raw), func(i int) error {
		var g map[string]any
		if err := json.Unmarshal(raw[i], &g); err != nil {
			return err
		}
		edit(g)
		b, err := json.Marshal(g)
		groups[i], raw[i] = b, nil
		return err
	})
	if err != nil {
		return nil, nil, err
	}
	return doc, groups, nil
}

// marshalGroups encodes each product group of doc alone and puts them back
// as raw JSON, so json.Marshal(doc) then writes the same bytes as before
// with only the top level left to encode.
func marshalGroups(doc map[string]any) error {
	products := doc["products"].([]any)
	raw := make([]json.RawMessage, len(products))
	err := each(len(products), func(i int) error {
		var err error
		raw[i], err = json.Marshal(products[i])
		return err
	})
	if err != nil {
		return err
	}
	doc["products"] = raw
	return nil
}
