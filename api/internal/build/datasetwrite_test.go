package build_test

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"strings"
	"testing"

	"github.com/shifter-io/atlas/api/internal/build"
	"github.com/shifter-io/atlas/api/internal/model"
	"github.com/shifter-io/atlas/api/internal/testscale"
)

// WriteDataset gives Marshal's bytes: with no, empty, nil and escaped
// providers, and with escaped text in the other fields.
func TestWriteDatasetEncodesLikeMarshal(t *testing.T) {
	t.Parallel()
	escaped := &model.Provider{Slug: "a<b>", Name: "R&D   \"quoted\""}
	for name, providers := range map[string][]*model.Provider{"none": nil, "empty": {}, "nil entry": {nil, escaped}, "two": {escaped, {Slug: "b"}}} {
		d := &model.Dataset{SchemaVersion: "<v>", GeneratedAt: "2026-11-04T00:00:00.000Z", Providers: providers,
			NA: []string{"x&y"}, Tape: []model.TapeEvent{{ID: "e1", Title: "<script>"}}}
		want, err := json.Marshal(d)
		if err != nil {
			t.Fatal(err)
		}
		var got bytes.Buffer
		if err := build.WriteDataset(&got, d); err != nil || !bytes.Equal(got.Bytes(), want) {
			t.Errorf("%s: %v\n got %s\nwant %s", name, err, got.Bytes(), want)
		}
	}
}

type failWriter struct{}

func (failWriter) Write([]byte) (int, error) { return 0, errors.New("disk full") }

// A write error and a value encoding/json refuses are errors, as they are
// for Marshal; the error names the field.
func TestWriteDatasetReportsErrors(t *testing.T) {
	t.Parallel()
	if err := build.WriteDataset(failWriter{}, &model.Dataset{}); err == nil || err.Error() != "disk full" {
		t.Fatalf("write error: %v", err)
	}
	broken := model.Measured{Pricing: model.Pricing{Plans: []model.PricingPlan{{Catalog: json.RawMessage("{")}}}}
	bad := &model.Dataset{Providers: []*model.Provider{{Slug: "a"}, {Slug: "b", Measured: broken}}}
	if _, err := json.Marshal(bad); err == nil {
		t.Fatal("Marshal accepts the value: pick another one")
	}
	if err := build.WriteDataset(io.Discard, bad); err == nil || !strings.HasPrefix(err.Error(), "dataset providers: ") {
		t.Fatalf("unsupported value: %v", err)
	}
	pages := &model.Dataset{Pages: build.DatasetPages{"/v1/networks": json.RawMessage("{")}}
	if err := build.WriteDataset(io.Discard, pages); err == nil || !strings.HasPrefix(err.Error(), "dataset pages: ") {
		t.Fatalf("unsupported page: %v", err)
	}
}

// A v4 dataset's lists give the bytes of encoding the dataset whole, and a
// list of 2,000 City x ASN rows holds its total, its 40 pages and only its
// first page (DATASET-SIZE-2).
func TestWriteDatasetWritesEachListsFirstPage(t *testing.T) {
	t.Parallel()
	in, err := testscale.NetworkInputs(testscale.NetworkShape{Shape: testscale.Shape{Networks: 1, Countries: 1, Exits: 3, Tail: 5,
		CityAsn: 2000}, Requested: 2})
	if err != nil {
		t.Fatal(err)
	}
	res, err := build.Build(in)
	if err != nil {
		t.Fatal(err)
	}
	var got bytes.Buffer
	if err := build.WriteDataset(&got, res.Dataset); err != nil {
		t.Fatal(err)
	}
	want, err := json.Marshal(res.Dataset)
	if err != nil || !bytes.Equal(got.Bytes(), want) {
		t.Fatalf("WriteDataset differs from Marshal (%v)", err)
	}
	var doc struct {
		Lists map[string]map[string]map[string]map[string]struct {
			Total int               `json:"total"`
			Pages int               `json:"pages"`
			Rows  []json.RawMessage `json:"rows"`
		} `json:"lists"`
	}
	if err := json.Unmarshal(got.Bytes(), &doc); err != nil {
		t.Fatal(err)
	}
	for slug, kinds := range doc.Lists {
		for kind, ranges := range kinds {
			if l := ranges["30d"]["city_asn"]; l.Total != 2000 || l.Pages != 40 || len(l.Rows) != build.PageSize {
				t.Errorf("%s %s: City x ASN total %d, %d pages, %d rows", slug, kind, l.Total, l.Pages, len(l.Rows))
			}
		}
	}
	if len(doc.Lists) != 1 {
		t.Errorf("%d networks in the lists", len(doc.Lists))
	}
}
