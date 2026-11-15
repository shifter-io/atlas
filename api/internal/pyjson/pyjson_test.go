package pyjson

import (
	"bytes"
	"encoding/json"
	"os"
	"strconv"
	"testing"
)

// vectors is testdata/vectors.json, written by Python 3.12: each document
// with json.dumps(json.loads(in), sort_keys=True, separators=(",", ":")),
// and floats' repr, from the edges of the fixed notation to random bit
// patterns.
type vectors struct {
	Documents []struct{ In, Out string }
	Floats    []string
}

func readVectors(t *testing.T) vectors {
	t.Helper()
	body, err := os.ReadFile("testdata/vectors.json")
	if err != nil {
		t.Fatal(err)
	}
	var v vectors
	if err := json.Unmarshal(body, &v); err != nil {
		t.Fatal(err)
	}
	if len(v.Documents) < 10 || len(v.Floats) < 1000 {
		t.Fatalf("%d documents, %d floats", len(v.Documents), len(v.Floats))
	}
	return v
}

func TestCanonicalIsPythonsJSONDumps(t *testing.T) {
	t.Parallel()
	for _, d := range readVectors(t).Documents {
		got, err := Canonical([]byte(d.In))
		if err != nil || string(got) != d.Out {
			t.Errorf("%s:\n got %s (%v)\nwant %s", d.In, got, err, d.Out)
		}
	}
}

func TestFloatReprIsPythonsRepr(t *testing.T) {
	t.Parallel()
	for _, want := range readVectors(t).Floats {
		f, err := strconv.ParseFloat(want, 64)
		if err != nil {
			t.Fatal(err)
		}
		if got := FloatRepr(f); got != want {
			t.Errorf("FloatRepr(%v) = %s, Python %s", f, got, want)
		}
	}
	for _, c := range []struct {
		in   float64
		want string
	}{{1e15, "1000000000000000.0"}, {1e16, "1e+16"}, {1e-4, "0.0001"}, {1e-5, "1e-05"}, {-2.5e-7, "-2.5e-07"}, {30, "30.0"}} {
		if got := FloatRepr(c.in); got != c.want {
			t.Errorf("FloatRepr(%v) = %s, want %s", c.in, got, c.want)
		}
	}
}

// The framework's configs/methodology.toml as policy.load() hashes it
// (testdata/methodology.json, written by Python) is canonical, and stays
// so in any layout the database prints it in.
func TestTheMethodologyFileIsCanonical(t *testing.T) {
	t.Parallel()
	body, err := os.ReadFile("testdata/methodology.json")
	if err != nil {
		t.Fatal(err)
	}
	var spaced bytes.Buffer
	if err := json.Indent(&spaced, body, "", "  "); err != nil {
		t.Fatal(err)
	}
	for _, doc := range [][]byte{body, spaced.Bytes()} {
		if got, err := Canonical(doc); err != nil || !bytes.Equal(got, body) {
			t.Errorf("canonical form differs (%v):\n%s", err, got)
		}
	}
}

func TestCanonicalRefusesWhatIsNotOneJSONValue(t *testing.T) {
	t.Parallel()
	for _, doc := range []string{"", "{", `{"a":1} {"b":2}`, "[1e999]"} {
		if got, err := Canonical([]byte(doc)); err == nil {
			t.Errorf("%q: %s, want an error", doc, got)
		}
	}
}
