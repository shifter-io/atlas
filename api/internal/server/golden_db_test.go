package server

import (
	"bytes"
	"encoding/json"
	"os"
	"strings"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/build"
	"github.com/Zeus-Development/proxy-index/api/internal/config"
	"github.com/Zeus-Development/proxy-index/api/internal/testdb"
	"github.com/Zeus-Development/proxy-index/api/internal/testscale"
)

// v4GoldenFile holds, per committed fixture published as a v4 build
// (testdb.PublishV4), the SHA-256 of every answer, written by this test with
// GOLDEN_WRITE=<path>. The dataset's and details' hashes are of their bytes
// without droppedKeys. Until payload v4 was the only one read, this test
// showed them equal to the answers the API gave on main before browser
// targets (BP-API) after testscale.AsV4Answers, with each window's lists
// from the one source a v4 build has (testscale.Listed).
const v4GoldenFile = "testdata/answers-v4.json"

// newTargetFields is what BP-API adds to a network target's dataset entry:
// its engine and its (null) group. Nothing else may differ.
var newTargetFields = []byte(`,"engine":"http","group":null`)

type golden struct {
	Main    string                       `json:"main"`
	Answers map[string]map[string]string `json:"answers"`
}

// goldenPages are answers rendered on request.
var goldenPages = []string{"/v1/networks/shifter/places?dimension=city", "/v1/networks/shifter/places?dimension=asn&range=7d",
	"/v1/countries/US/cities", "/v1/networks/all/places?dimension=city&range=24h", "/v1/search?q=shift", "/v1/events"}

// goldenFixtures are the committed fixtures, by golden name.
var goldenFixtures = map[string]string{"v1": "snapshot.json", "every-request": "snapshot-every-request.json"}

// strippedAnswers is every answer of the database's current build, as
// renderedAnswers renders them, with newTargetFields and what LAT-MEAN-API
// adds without a latency mean removed. /v1/version is left out: its revision
// is the dataset's hash, which is compared itself.
func strippedAnswers(t *testing.T, db *testdb.DB, id int64) map[string][]byte {
	t.Helper()
	out := renderedAnswers(t, db, id, goldenPages, func(res *build.Result) { withoutLatencyMeanGaps(res.Coverage) })
	delete(out, versionPath)
	for _, name := range []string{"dataset", "details"} {
		out[name] = withoutKeys(t, out[name], droppedKeys)
	}
	for name, b := range out {
		out[name] = testscale.WithoutLatencyMean(bytes.ReplaceAll(b, newTargetFields, nil))
	}
	return out
}

// A snapshot without browser entries gives every answer byte for byte as
// main did before browser targets, apart from the two fields each network
// target gains in the dataset's target list (engine "http", group null): the
// committed fixtures, published to a database with the framework's targets
// as v4 builds, compared with v4GoldenFile.
func TestWithoutBrowserEntriesEveryAnswerIsAsBefore(t *testing.T) {
	t.Parallel()
	got := golden{Main: os.Getenv("GOLDEN_MAIN"), Answers: map[string]map[string]string{}}
	for name, file := range goldenFixtures {
		snapshot, err := os.ReadFile(config.FindUp("fixtures/measured/" + file))
		if err != nil {
			t.Fatal(err)
		}
		db := testdb.New(t)
		v4 := strippedAnswers(t, db, db.PublishV4(t, snapshot))
		got.Answers[name] = sums(v4)
	}
	if path := os.Getenv("GOLDEN_WRITE"); path != "" {
		b, err := json.MarshalIndent(got, "", " ")
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, append(b, '\n'), 0o644); err != nil {
			t.Fatal(err)
		}
		return
	}
	sameGolden(t, readGolden(t, v4GoldenFile), got)
}

// readGolden is the golden file at path.
func readGolden(t *testing.T, path string) golden {
	t.Helper()
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var g golden
	if err := json.Unmarshal(raw, &g); err != nil {
		t.Fatal(err)
	}
	return g
}

// sameGolden reports each of got's answers whose hash differs from want's,
// for every fixture got has.
func sameGolden(t *testing.T, want, got golden) {
	t.Helper()
	for name, answers := range got.Answers {
		if _, ok := want.Answers[name]; !ok {
			t.Errorf("%s: not in the golden of main %s", name, want.Main)
			continue
		}
		t.Run(name, func(t *testing.T) { sameSums(t, want.Answers[name], answers) })
	}
}

// withoutLatencyMeanGaps takes the performance.latencyMean N/A paths that
// LAT-MEAN-API adds out of the coverage report, with their counts, so the
// report is the one built before it.
func withoutLatencyMeanGaps(c *build.Coverage) {
	gap := func(path string) bool { return strings.HasSuffix(path, "performance.latencyMean") }
	for i := range c.Providers {
		p := &c.Providers[i]
		for name, fields := range p.Sections {
			kept := []build.CoverageField{}
			for _, f := range fields {
				if !gap(f.Path) {
					kept = append(kept, f)
				}
			}
			gone := len(fields) - len(kept)
			p.NACount -= gone
			c.Summary.NAFields -= gone
			c.Summary.NAFieldsBySource[p.Source] -= gone
			p.Sections[name] = kept
		}
	}
	for pt := range c.Fields {
		if gap(pt) {
			delete(c.Fields, pt)
		}
	}
}

// droppedKeys are the members DATASET-SIZE-1 took out of the dataset and its
// providers (measurement, placeRanks) or added to them (pages,
// coverageEvidence), and the lists API-DATASET-2 added. They are removed from
// the dataset and details before hashing, so every other byte of them still
// has to match main.
var droppedKeys = map[string]bool{"measurement": true, "placeRanks": true, "pages": true, "coverageEvidence": true, "lists": true}

// withoutKeys is the compact JSON b without the members named in keys, at
// any depth, every other byte as written (encoding/json writes what it reads
// back).
func withoutKeys(t *testing.T, b []byte, keys map[string]bool) []byte {
	t.Helper()
	dec := json.NewDecoder(bytes.NewReader(b))
	dec.UseNumber()
	var out bytes.Buffer
	if err := copyJSON(dec, &out, keys); err != nil {
		t.Fatal(err)
	}
	return out.Bytes()
}

func copyJSON(dec *json.Decoder, out *bytes.Buffer, keys map[string]bool) error {
	tok, err := dec.Token()
	if err != nil {
		return err
	}
	switch tok {
	case json.Delim('{'):
		out.WriteByte('{')
		for n := 0; dec.More(); {
			key, err := dec.Token()
			if err != nil {
				return err
			}
			if keys[key.(string)] {
				var skip json.RawMessage
				if err := dec.Decode(&skip); err != nil {
					return err
				}
				continue
			}
			if n++; n > 1 {
				out.WriteByte(',')
			}
			k, _ := json.Marshal(key) // a string always encodes
			out.Write(append(k, ':'))
			if err := copyJSON(dec, out, keys); err != nil {
				return err
			}
		}
		out.WriteByte('}')
	case json.Delim('['):
		out.WriteByte('[')
		for n := 0; dec.More(); n++ {
			if n > 0 {
				out.WriteByte(',')
			}
			if err := copyJSON(dec, out, keys); err != nil {
				return err
			}
		}
		out.WriteByte(']')
	default:
		v, err := json.Marshal(tok)
		if err != nil {
			return err
		}
		out.Write(v)
		return nil
	}
	_, err = dec.Token() // the closing delimiter
	return err
}
