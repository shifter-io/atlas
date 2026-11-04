package build_test

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"strings"
	"testing"

	"github.com/Zeus-Development/proxy-index-website/api/internal/build"
	"github.com/Zeus-Development/proxy-index-website/api/internal/model"
)

// WriteDataset gives Marshal's bytes: with no, empty, nil and escaped
// providers, and with escaped text in the other fields.
func TestWriteDatasetEncodesLikeMarshal(t *testing.T) {
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
}
