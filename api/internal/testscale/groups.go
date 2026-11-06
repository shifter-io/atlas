package testscale

import (
	"encoding/json"
	"maps"
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
	groups, err := editGroups(raw, edit, true)
	if err != nil {
		return nil, nil, err
	}
	return doc, groups, nil
}

// editGroups decodes each group alone, applies edit and encodes it again, in
// order; with release, each group's input is dropped once it is done.
func editGroups(raw []json.RawMessage, edit func(map[string]any), release bool) ([][]byte, error) {
	groups := make([][]byte, len(raw))
	err := each(len(raw), func(i int) error {
		var g map[string]any
		if err := json.Unmarshal(raw[i], &g); err != nil {
			return err
		}
		edit(g)
		b, err := json.Marshal(g)
		groups[i] = b
		if release {
			raw[i] = nil
		}
		return err
	})
	return groups, err
}

// marshalGroups applies edit to each product group of doc, encodes it
// alone and puts them back as raw JSON, so json.Marshal(doc) then writes the
// same bytes as before with only the top level left to encode.
func marshalGroups(doc map[string]any, edit func(map[string]any)) error {
	products := doc["products"].([]any)
	raw := make([]json.RawMessage, len(products))
	err := each(len(products), func(i int) error {
		edit(products[i].(map[string]any))
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

// Groups is a payload split as Parts reads it: its top level without the
// product groups, and each group encoded alone.
type Groups struct {
	doc    map[string]json.RawMessage
	groups []json.RawMessage
}

// CurrentGroups is the shape's payload as Current writes it, already split:
// the same groups as Parts(Current(payload)) reads, where payload is
// Inputs(shape)'s, without encoding the whole payload and decoding it twice
// (for a QA-1-sized build, most of the time it takes).
func CurrentGroups(shape Shape) (*Groups, error) {
	doc, _, err := shapeDoc(shape, current)
	if err != nil {
		return nil, err
	}
	g := &Groups{doc: map[string]json.RawMessage{}, groups: doc["products"].([]json.RawMessage)}
	for k, v := range doc {
		if k != "products" {
			if g.doc[k], err = json.Marshal(v); err != nil {
				return nil, err
			}
		}
	}
	return g, nil
}

// Parts is Parts(payload, sparse) of the split payload. It changes nothing
// in g, so calls can run at once.
func (g *Groups) Parts(sparse bool) (head []byte, products, runs [][]byte, err error) {
	edit := func(map[string]any) {}
	if sparse {
		edit = sparseSeries
	}
	if products, err = editGroups(g.groups, edit, false); err != nil {
		return nil, nil, nil, err
	}
	head, runs, err = headAndRuns(maps.Clone(g.doc), len(products))
	return head, products, runs, err
}

// headAndRuns is the head of a payload in parts (its top level but products
// and runs, plus "parts") and its runs parts, from the payload's top level
// doc, which it changes, and its product group count.
func headAndRuns(doc map[string]json.RawMessage, products int) (head []byte, runs [][]byte, err error) {
	var all []json.RawMessage
	if doc["runs"] != nil {
		if err := json.Unmarshal(doc["runs"], &all); err != nil {
			return nil, nil, err
		}
	}
	for i := 0; i < len(all); i += RunsPerPart {
		b, err := json.Marshal(all[i:min(i+RunsPerPart, len(all))])
		if err != nil {
			return nil, nil, err
		}
		runs = append(runs, b)
	}
	delete(doc, "products")
	delete(doc, "runs")
	if doc["parts"], err = json.Marshal(map[string]int{"products": products, "runs": len(runs)}); err != nil {
		return nil, nil, err
	}
	head, err = json.Marshal(doc)
	return head, runs, err
}
