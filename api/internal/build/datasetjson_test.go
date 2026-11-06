package build_test

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/build"
	"github.com/Zeus-Development/proxy-index/api/internal/testscale"
)

// pair is one member of a JSON object, its value as written.
type pair struct {
	key string
	raw json.RawMessage
}

// pairs is a JSON object's members in their written order.
func pairs(t *testing.T, raw []byte) []pair {
	t.Helper()
	dec := json.NewDecoder(bytes.NewReader(raw))
	if tok, err := dec.Token(); err != nil || tok != json.Delim('{') {
		t.Fatalf("not an object: %v %v", tok, err)
	}
	var out []pair
	for dec.More() {
		tok, err := dec.Token()
		if err != nil {
			t.Fatal(err)
		}
		var v json.RawMessage
		if err := dec.Decode(&v); err != nil {
			t.Fatal(err)
		}
		out = append(out, pair{key: tok.(string), raw: v})
	}
	return out
}

// joinPairs writes members back as one compact object.
func joinPairs(members []pair) []byte {
	var b bytes.Buffer
	b.WriteByte('{')
	for i, m := range members {
		if i > 0 {
			b.WriteByte(',')
		}
		key, _ := json.Marshal(m.key)
		b.Write(key)
		b.WriteByte(':')
		b.Write(m.raw)
	}
	b.WriteByte('}')
	return b.Bytes()
}

// goldenShape is the small build whose retained dataset fields are pinned:
// two networks in two requested countries, the first group of each holding a
// whole city x ASN list.
var goldenShape = testscale.Shape{Networks: 2, Countries: 2, Exits: 3, Tail: 30, Unmeasured: 1, Events: 5, CityAsn: 4}

// datasetFile is the dataset file of a build of shape, and the build.
func datasetFile(t *testing.T, shape testscale.Shape) ([]byte, *build.Result) {
	t.Helper()
	in, err := testscale.Inputs(shape)
	if err != nil {
		t.Fatal(err)
	}
	res, err := build.Build(in)
	if err != nil {
		t.Fatal(err)
	}
	var file bytes.Buffer
	if err := build.WriteDataset(&file, res.Dataset); err != nil {
		t.Fatal(err)
	}
	return file.Bytes(), res
}

// Every field kept in the dataset file is byte for byte what it was before
// `measurement` and `placeRanks` left it (DATASET-SIZE-1): the golden is the
// file the earlier code wrote for goldenShape, with those two provider
// fields and the N/A paths under placeRanks taken out. The new `pages` and
// `coverageEvidence` are left out of the comparison; their own tests check
// them. goldenShape is a build before v4, so its `lists` is {}. Its snapshot
// has no latency mean, so what LAT-MEAN-API adds for it is left out too.
func TestTheKeptDatasetFieldsAreUnchanged(t *testing.T) {
	t.Parallel()
	file, _ := datasetFile(t, goldenShape)
	file = testscale.WithoutLatencyMean(file)
	top := []pair{}
	for _, kv := range pairs(t, file) {
		if kv.key == "lists" && string(kv.raw) != "{}" {
			t.Errorf("a build before v4 has lists %.200s", kv.raw)
		}
		if kv.key == "pages" || kv.key == "lists" {
			continue
		}
		if kv.key == "providers" {
			var providers []json.RawMessage
			if err := json.Unmarshal(kv.raw, &providers); err != nil {
				t.Fatal(err)
			}
			kept := [][]byte{}
			for _, p := range providers {
				fields := []pair{}
				for _, f := range pairs(t, p) {
					if f.key != "coverageEvidence" {
						fields = append(fields, f)
					}
				}
				kept = append(kept, joinPairs(fields))
			}
			kv.raw = append(append([]byte{'['}, bytes.Join(kept, []byte{','})...), ']')
		}
		top = append(top, kv)
	}
	want, err := os.ReadFile(filepath.Join("testdata", "dataset-retained.golden.json"))
	if err != nil {
		t.Fatal(err)
	}
	if got := joinPairs(top); !bytes.Equal(got, want) {
		at := 0
		for at < min(len(got), len(want)) && got[at] == want[at] {
			at++
		}
		t.Fatalf("kept fields differ from the golden at byte %d: got ...%s..., want ...%s...", at,
			got[max(0, at-80):min(len(got), at+80)], want[max(0, at-80):min(len(want), at+80)])
	}
}

// preV4File is the SHA-256 of goldenShape's dataset file as the base of
// API-DATASET-2 writes it (computed there).
const preV4File = "557cf8117404a621f49101125b055769e293f90bf08480f74accbb5e8b032d9e"

// A build before v4 writes the file it wrote before the lists, plus
// "lists":{} (regression case 3 of API-DATASET-2), and what LAT-MEAN-API
// adds without a latency mean.
func TestAPreV4FileIsTheEarlierFileWithEmptyLists(t *testing.T) {
	t.Parallel()
	file, _ := datasetFile(t, goldenShape)
	file = testscale.WithoutLatencyMean(file)
	i := bytes.Index(file, []byte(`,"lists":{},"snapshot":`))
	if i < 0 {
		t.Fatal(`no "lists":{} before the snapshot`)
	}
	earlier := append(append([]byte{}, file[:i]...), file[i+len(`,"lists":{}`):]...)
	if sum := sha256.Sum256(earlier); hex.EncodeToString(sum[:]) != preV4File {
		t.Errorf("the pre-v4 file without its empty lists has SHA-256 %x, the earlier file %s", sum, preV4File)
	}
}
