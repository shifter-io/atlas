package build

import (
	"bytes"
	"encoding/json"
	"errors"
	"strings"
	"testing"

	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/snap"
)

// A network kind without lists or a Coverage list writes every range and
// dimension as an empty list; a published group whose provider has no
// network has no lists; a build without a snapshot has none at all.
func TestDatasetListsOfNetworksWithoutRows(t *testing.T) {
	t.Parallel()
	d := &DatasetLists{networks: []*datasetNetwork{{slug: "net", kinds: []*datasetKind{{kind: "residential"}}}}}
	b, err := json.Marshal(d)
	none := func(columns string) string { return `{"total":0,"pages":0,"columns":[` + columns + `],"rows":[]}` }
	counts := `"ipsSum","requests","successes","latencyP50"`
	empty := `{"country":` + none(`"countryCode",`+counts) + `,"region":` + none(`"countryCode","regionCode","region",`+counts) +
		`,"city":` + none(`"countryCode","regionCode","region","city",`+counts) + `,"asn":` + none(`"asn","asnName",`+counts) +
		`,"city_asn":` + none(`"countryCode","region","city","asn","asnName","usageType","ips","latencyP50","latencyRank","latencyPeers","ipsRank","ipsPeers"`) + `}`
	if want := `{"net":{"residential":{"24h":` + empty + `,"7d":` + empty + `,"30d":` + empty + `}}}`; err != nil || string(b) != want {
		t.Errorf("%v: %s", err, b)
	}
	s := &snap.Snapshot{Products: []snap.Product{{Provider: "unlisted", Kind: "residential"}}}
	if l := datasetLists(&Pages{}, s, nil); len(l.networks) != 0 {
		t.Errorf("networks %v", l.networks)
	}
	if b, _ := json.Marshal(datasetLists(&Pages{}, nil, nil)); string(b) != "{}" {
		t.Errorf("a build without a snapshot: %s", b)
	}
}

// A list's counts are its whole list's and its rows only the first page: a
// list of 51 rows is two pages, of which the file holds the 50 the build
// keeps.
func TestADatasetListHoldsItsFirstPage(t *testing.T) {
	t.Parallel()
	first := make([]PlaceRow, PageSize)
	for i := range first {
		first[i] = PlaceRow{CountryCode: model.S("AA"), IpsSum: Num(100 - i), Requests: nan, Successes: 1, LatencyP50: nan}
	}
	l := placeFirst(&placeIndex{srcs: []placeSource{}, total: PageSize + 1, first: first}, "country", nil)
	b, err := json.Marshal(l)
	if err != nil || l.Total != PageSize+1 || l.Pages != 2 || len(l.Rows) != PageSize ||
		!strings.HasPrefix(string(b), `{"total":51,"pages":2,"columns":["countryCode","ipsSum","requests","successes","latencyP50"],"rows":[["AA",100,null,1,null],["AA"`) {
		t.Errorf("%v: %.120s", err, b)
	}
	if c := coverageFirst(&coverageList{}); c.Total != 0 || c.Rows == nil {
		t.Errorf("a Coverage list without its first page: %+v", c)
	}
	scored := placeFirst(nil, "asn", func(PlaceRow) *model.MetricScore { return nil })
	if got := strings.Join(scored.Columns, " "); got != "asn asnName ipsSum requests successes latencyP50 successScore" ||
		len(placeColumns["asn"]) != 6 {
		t.Errorf("a scored list's columns: %s (the dimension's %v)", got, placeColumns["asn"])
	}
}

// Every place row has as many values as its list has columns, the Coverage
// row has the country's name (#992) and the dataset file's has none
// (PLACE-CODES-1).
func TestPlaceRowsMatchTheirColumns(t *testing.T) {
	t.Parallel()
	for dim, cols := range placeColumns {
		if row := placeArray(dim, PlaceRow{}); len(row) != len(cols) {
			t.Errorf("%s: %d values, %d columns", dim, len(row), len(cols))
		}
	}
	r := CoverageRow{CoverageRow: model.CoverageRow{CountryCode: model.S("DE"), Country: model.S("Germany"), City: model.S("Berlin")}}
	served, file := r.cells(true), r.cells(false)
	if len(served) != len(coverageColumns) || served[1] != r.Country || len(file) != len(datasetCoverageColumns) || file[2] != r.City {
		t.Errorf("coverage cells: served %v, file %v", served, file)
	}
	if b, err := json.Marshal(r); err != nil || !strings.HasPrefix(string(b), `["DE","Germany",null,"Berlin"`) {
		t.Errorf("a Coverage row: %v %s", err, b)
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
		k.places[0][0] = &placeIndex{srcs: []placeSource{}, total: 1, first: []PlaceRow{{CountryCode: &big}}}
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
