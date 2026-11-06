package testscale

import (
	"bytes"
	"slices"
	"testing"
)

// CurrentGroups' parts are byte for byte Parts(Current(payload)) of Inputs'
// payload, with the daily groups' series sparse and not, and calls at once
// share nothing (go test -race).
func TestCurrentGroupsAreTheCurrentPayloadsParts(t *testing.T) {
	t.Parallel()
	shape := Shape{Networks: 2, Countries: 2, Exits: 3, Tail: 30, Unmeasured: 1, Events: 5, Browser: true, CityAsn: 10}
	in, err := Inputs(shape)
	if err != nil {
		t.Fatal(err)
	}
	payload, err := Current(in.Snapshot.Payload)
	if err != nil {
		t.Fatal(err)
	}
	g, err := CurrentGroups(shape)
	if err != nil {
		t.Fatal(err)
	}
	for _, sparse := range []bool{false, true} {
		t.Run(map[bool]string{false: "whole", true: "sparse"}[sparse], func(t *testing.T) {
			t.Parallel()
			head, products, runs, err := Parts(payload, sparse)
			if err != nil {
				t.Fatal(err)
			}
			gotHead, gotProducts, gotRuns, err := g.Parts(sparse)
			if err != nil {
				t.Fatal(err)
			}
			if !bytes.Equal(gotHead, head) {
				t.Errorf("head differs:\n%.300s\nwant\n%.300s", gotHead, head)
			}
			if len(products) != shape.Networks*shape.Countries || !slices.EqualFunc(gotProducts, products, bytes.Equal) || !slices.EqualFunc(gotRuns, runs, bytes.Equal) {
				t.Errorf("%d products, %d runs parts; want %d and %d, the same bytes", len(gotProducts), len(gotRuns), len(products), len(runs))
			}
		})
	}
}
