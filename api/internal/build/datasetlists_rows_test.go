package build

import (
	"bufio"
	"bytes"
	"encoding/json"
	"errors"
	"strings"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/Zeus-Development/proxy-index/api/internal/snap"
	"github.com/Zeus-Development/proxy-index/api/internal/store"
)

// rowsOf is n rows written by a rowWriter, and its error.
func rowsOf(n int, row func(int) any) (string, error) {
	var b bytes.Buffer
	w := bufio.NewWriter(&b)
	rw := &rowWriter{w: w}
	for i := range n {
		rw.add(row(i))
	}
	err := rw.close()
	w.Flush() //nolint:errcheck // a bytes.Buffer
	return b.String(), err
}

// A rowWriter's array is the array encoding gives the rows whole: none, one
// row, exactly one chunk, a chunk and one row; and a row encoding refuses is
// an error.
func TestTheRowWriterWritesOneArrayInChunks(t *testing.T) {
	t.Parallel()
	for _, n := range []int{0, 1, listChunk, listChunk + 1, 2*listChunk + 7} {
		rows := make([]any, n)
		for i := range rows {
			rows[i] = [...]any{"a<b", i, model.Num(nan)}
		}
		want, _ := json.Marshal(rows)
		if got, err := rowsOf(n, func(i int) any { return rows[i] }); err != nil || got != string(want) {
			t.Errorf("%d rows: %v, got %.120s", n, err, got)
		}
	}
	if _, err := rowsOf(3, func(i int) any { return json.RawMessage("{") }); err == nil {
		t.Error("a row encoding refuses is not an error")
	}
}

// A network kind without lists or a Coverage list writes every range and
// dimension as an empty array; a published group whose provider has no
// network has no rows; a build before v4 has none at all.
func TestDatasetListsOfNetworksWithoutRows(t *testing.T) {
	t.Parallel()
	d := &DatasetLists{networks: []*datasetNetwork{{slug: "net", kinds: []*datasetKind{{kind: "residential"}}}}}
	b, err := json.Marshal(d)
	empty := `{"country":[],"region":[],"city":[],"asn":[],"city_asn":[]}`
	if want := `{"net":{"residential":{"24h":` + empty + `,"7d":` + empty + `,"30d":` + empty + `}}}`; err != nil || string(b) != want {
		t.Errorf("%v: %s", err, b)
	}
	s := &snap.Snapshot{Products: []snap.Product{{Provider: "unlisted", Kind: "residential"}}}
	row := &store.SnapshotRow{Networks: map[store.PlaceWindow]*store.NetList{}}
	for _, pg := range []*Pages{{v4: true}, {v4: false}} {
		if l := datasetLists(pg, s, nil, row); len(l.networks) != 0 {
			t.Errorf("v4 %v: networks %v", pg.v4, l.networks)
		}
	}
	if b, _ := json.Marshal(datasetLists(&Pages{}, nil, nil, nil)); string(b) != "{}" {
		t.Errorf("a build before v4: %s", b)
	}
}

// failAfter fails every write once n bytes are written.
type failAfter struct{ n int }

func (f *failAfter) Write(p []byte) (int, error) {
	if f.n -= len(p); f.n < 0 {
		return 0, errDiskFull
	}
	return len(p), nil
}

var errDiskFull = errors.New("disk full")

// A write that fails while the lists are written is an error naming them.
func TestWriteDatasetReportsAListsWriteError(t *testing.T) {
	t.Parallel()
	d := &model.Dataset{Lists: &DatasetLists{}}
	for range 40 {
		k := &datasetKind{kind: "residential"}
		big := strings.Repeat("x", 4096)
		k.srcs[0] = []placeSource{{list: oneRowList(t, big), scale: nan}}
		d.Lists.(*DatasetLists).networks = append(d.Lists.(*DatasetLists).networks, &datasetNetwork{slug: big[:8], kinds: []*datasetKind{k}})
	}
	var whole bytes.Buffer
	if err := WriteDataset(&whole, &model.Dataset{}); err != nil {
		t.Fatal(err)
	}
	if err := WriteDataset(&failAfter{n: whole.Len()}, d); err == nil || !strings.HasPrefix(err.Error(), "dataset lists: ") {
		t.Errorf("write error: %v", err)
	}
}

// oneRowList is a network list of one country row named name.
func oneRowList(t *testing.T, name string) *store.NetList {
	t.Helper()
	l, err := store.NewNetList(map[string][]store.NetRow{"country": {{Place: &store.Place{CountryCode: model.S("AA"), Country: &name}, Ips: 1,
		Completed: 1, Successes: 1, P50: 1}}})
	if err != nil {
		t.Fatal(err)
	}
	return l
}
