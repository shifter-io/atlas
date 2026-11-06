package build

import (
	"bytes"
	"encoding/json"
	"errors"
	"strings"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/Zeus-Development/proxy-index/api/internal/snap"
)

// A network kind without lists or a Coverage list writes every range and
// dimension as an empty list; a published group whose provider has no
// network has no lists; a build before v4 has none at all.
func TestDatasetListsOfNetworksWithoutRows(t *testing.T) {
	t.Parallel()
	d := &DatasetLists{networks: []*datasetNetwork{{slug: "net", kinds: []*datasetKind{{kind: "residential"}}}}}
	b, err := json.Marshal(d)
	none := `{"total":0,"pages":0,"rows":[]}`
	empty := `{"country":` + none + `,"region":` + none + `,"city":` + none + `,"asn":` + none + `,"city_asn":` + none + `}`
	if want := `{"net":{"residential":{"24h":` + empty + `,"7d":` + empty + `,"30d":` + empty + `}}}`; err != nil || string(b) != want {
		t.Errorf("%v: %s", err, b)
	}
	s := &snap.Snapshot{Products: []snap.Product{{Provider: "unlisted", Kind: "residential"}}}
	for _, pg := range []*Pages{{v4: true}, {v4: false}} {
		if l := datasetLists(pg, s, nil); len(l.networks) != 0 {
			t.Errorf("v4 %v: networks %v", pg.v4, l.networks)
		}
	}
	if b, _ := json.Marshal(datasetLists(&Pages{}, nil, nil)); string(b) != "{}" {
		t.Errorf("a build before v4: %s", b)
	}
}

// A list's counts are its whole list's and its rows only the first page: a
// list of 51 rows is two pages, of which the file holds the 50 the build
// keeps.
func TestADatasetListHoldsItsFirstPage(t *testing.T) {
	t.Parallel()
	first := make([]PlaceRow, PageSize)
	for i := range first {
		first[i] = PlaceRow{CountryCode: model.S("AA"), Country: model.S("Aa"), IpsSum: Num(100 - i), Requests: nan, Successes: 1, LatencyP50: nan}
	}
	l := placeFirst(&placeIndex{srcs: []placeSource{}, total: PageSize + 1, first: first}, "country")
	b, err := json.Marshal(l)
	if err != nil || l.Total != PageSize+1 || l.Pages != 2 || len(l.Rows) != PageSize ||
		!strings.HasPrefix(string(b), `{"total":51,"pages":2,"rows":[["AA","Aa",100,null,1,null],["AA"`) {
		t.Errorf("%v: %.120s", err, b)
	}
	if c := coverageFirst(&coverageList{}); c.Total != 0 || c.Rows == nil {
		t.Errorf("a Coverage list without its first page: %+v", c)
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
		k.places[0][0] = &placeIndex{srcs: []placeSource{}, total: 1, first: []PlaceRow{{CountryCode: model.S("AA"), Country: &big}}}
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
