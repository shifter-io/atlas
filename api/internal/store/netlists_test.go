package store

import (
	"math/rand/v2"
	"reflect"
	"slices"
	"strconv"
	"strings"
	"sync"
	"testing"
)

// fake is one row given to a NetLists as the list query would return it.
type fake struct {
	code                      byte
	place, network            int32
	ips, completed, successes int64
	p50                       int32
}

// fakeList loads rows into a fresh NetLists the way Load does, with places
// and networks standing for place_names and asn_label_names.
func fakeList(t testing.TB, places map[int32]Place, networks map[int32]Network, rows []fake) (*NetLists, *NetList) {
	t.Helper()
	n, l := NewNetLists(), &NetList{}
	n.reset()
	for _, r := range rows {
		s := &scannedRow{id: 1, code: r.code, place: r.place, network: r.network}
		s.Ips, s.Completed, s.Successes, s.P50 = r.ips, r.completed, r.successes, r.p50
		if err := n.add(l, s); err != nil {
			t.Fatal(err)
		}
	}
	for id, p := range n.places {
		*p = places[id]
	}
	for id, w := range n.networks {
		*w = networks[id]
	}
	n.missing = nil
	n.fillTexts([2]int{})
	l.finish(n.names)
	return n, l
}

func num(v int64) *int64 { return &v }

var (
	testPlaces = map[int32]Place{
		1: {CountryCode: str("US"), Country: str("United States")},
		2: {CountryCode: str("US"), Country: str("United States"), RegionCode: str("CA"), Region: str("California"), City: str("San Jose")},
		3: {CountryCode: str("GB"), Country: str("United Kingdom"), City: str("London")},
		4: {CountryCode: str("DE"), Country: str("Germany"), City: str("Berlin")},
	}
	testNetworks = map[int32]Network{
		7: {Asn: num(64512), Name: str("Example Transit"), UsageType: str("isp")},
		8: {Asn: num(64496), Name: str("Edge Net")},
		9: {Name: str("Unknown")},
	}
)

func TestNetListRowsAndOrder(t *testing.T) {
	_, l := fakeList(t, testPlaces, testNetworks, []fake{
		{'y', 2, 7, 5, 10, 9, 70},
		{'y', 3, 8, 9, -1, -1, -1},
		{'y', 4, 7, 5, 3, 3, 40},
		{'y', 2, 8, 9, 1, 1, 30},
		{'a', -1, 9, 4, 4, 4, 20},
	})
	if l.Len("city_asn") != 4 || l.Len("asn") != 1 || l.Len("country") != 0 || l.Len("country_asn") != 0 || l.Len("nope") != 0 {
		t.Fatalf("lengths %d %d %d", l.Len("city_asn"), l.Len("asn"), l.Len("country"))
	}
	r := l.Row("city_asn", 1)
	if *r.Place.City != "London" || *r.Network.Asn != 64496 || r.Ips != 9 || r.Completed != -1 || r.Successes != -1 || r.P50 != -1 {
		t.Errorf("row 1 %+v", r)
	}
	if a := l.Row("asn", 0); a.Place != nil || *a.Network.Name != "Unknown" || a.Network.Asn != nil {
		t.Errorf("ASN row %+v", a)
	}
	// Ties keep their stored position: 1 before 3, 0 before 2.
	if got := l.ByIps("city_asn"); !slices.Equal(got, []int32{1, 3, 0, 2}) {
		t.Errorf("most IPs first %v", got)
	}
	var none *NetList
	if none.Len("city") != 0 || len(none.ByIps("city")) != 0 {
		t.Error("a nil list has rows")
	}
}

// Rows naming the same place or ASN share one name entry.
func TestNetListSharesNames(t *testing.T) {
	n, l := fakeList(t, testPlaces, testNetworks, []fake{{'y', 2, 7, 1, 1, 1, 1}, {'g', 2, -1, 1, 1, 1, 1}, {'a', -1, 7, 1, 1, 1, 1}})
	if len(n.names.places) != 1 || len(n.names.networks) != 1 || l.Row("city", 0).Place != l.Row("city_asn", 0).Place {
		t.Errorf("names %d places, %d networks", len(n.names.places), len(n.names.networks))
	}
}

func TestNetListSearch(t *testing.T) {
	_, l := fakeList(t, testPlaces, testNetworks, []fake{
		{'y', 2, 7, 5, 1, 1, 1}, // United States, San Jose, AS64512
		{'y', 3, 8, 9, 1, 1, 1}, // United Kingdom, London, AS64496
		{'y', 4, 7, 7, 1, 1, 1}, // Germany, Berlin, AS64512
		{'y', 1, 9, 2, 1, 1, 1}, // United States, no ASN number
	})
	for _, c := range []struct {
		q    string
		want []int32
	}{
		{"united st", []int32{0, 3}},
		{"  UNITED   Kingdom ", []int32{1}},
		{"as64512", []int32{0, 2}},
		{"64512", []int32{0, 2}},
		{"berlin as64512", []int32{2}},
		{"ca", []int32{0}}, // California; region codes are not searched
		{"transit jose", []int32{0}},
		{"unknown us", []int32{3}},
		{"paris", []int32{}},
		{"", []int32{0, 1, 2, 3}},
	} {
		if got := l.Match("city_asn", c.q, nil); !slices.Equal(got, c.want) {
			t.Errorf("%q: %v, want %v", c.q, got, c.want)
		}
	}
	if got := l.Match("city_asn", "united", l.ByIps("city_asn")); !slices.Equal(got, []int32{1, 0, 3}) {
		t.Errorf("in IPs order %v", got)
	}
	want := NetTotals{Ips: 16, Countries: 2, Cities: 2, Asns: 2}
	if got := l.Totals("city_asn", l.Match("city_asn", "united", nil)); got != want {
		t.Errorf("totals %+v, want %+v", got, want)
	}
}

func TestNetListCounterRange(t *testing.T) {
	n, l := NewNetLists(), &NetList{}
	n.reset()
	s := &scannedRow{id: 5, code: 'c', place: -1, network: -1}
	s.Ips, s.Completed = 1, null32-1
	if err := n.add(l, s); err != nil {
		t.Fatal(err)
	}
	s.Completed = null32
	if err := n.add(l, s); err == nil || !strings.Contains(err.Error(), "list 5: counter 4294967295") {
		t.Errorf("a counter past the row: %v", err)
	}
	l.finish(n.names)
	if r := l.Row("country", 0); r.Completed != null32-1 {
		t.Errorf("largest counter %d", r.Completed)
	}
}

// propertySeed is the recorded seed of the search and totals property test.
const propertySeed = 20261105

// Over random rows and queries, Match and Totals equal a brute-force pass
// over the rows' names.
func TestNetListSearchAndTotalsProperty(t *testing.T) {
	t.Logf("seed %d", propertySeed)
	rng := rand.New(rand.NewPCG(propertySeed, 0))
	words := strings.Fields("north south east west port lake hill river net link fast data")
	pick := func() string { return words[rng.IntN(len(words))] }
	places, networks := map[int32]Place{}, map[int32]Network{}
	for i := range int32(40) {
		cc := string(rune('A'+rng.IntN(5))) + "X"
		p := Place{CountryCode: str(cc), Country: str(pick() + " " + cc)}
		if rng.IntN(3) > 0 {
			p.Region, p.City = str(pick()), str(pick()+" "+pick())
		}
		places[i] = p
	}
	for i := range int32(15) {
		w := Network{Name: str(pick() + " " + pick())}
		if rng.IntN(4) > 0 {
			w.Asn = num(64500 + rng.Int64N(8))
		}
		networks[i] = w
	}
	var rows []fake
	for range 500 {
		rows = append(rows, fake{'y', rng.Int32N(40), rng.Int32N(15), rng.Int64N(1000), 1, 1, 1})
	}
	_, l := fakeList(t, places, networks, rows)
	for range 200 {
		q := pick()
		if rng.IntN(2) == 0 {
			q += " " + pick()
		}
		if rng.IntN(4) == 0 {
			q = "as6450" + strconv.Itoa(rng.IntN(8))
		}
		var want []int32
		brute := NetTotals{}
		countries, cities, asns := map[string]bool{}, map[*Place]bool{}, map[int64]bool{}
		for i := range l.Len("city_asn") {
			r := l.Row("city_asn", i)
			text := strings.ToLower(strings.Join([]string{*r.Place.CountryCode, *r.Place.Country, deref(r.Place.Region), deref(r.Place.City), deref(r.Network.Name)}, " "))
			if r.Network.Asn != nil {
				text += " as" + strconv.FormatInt(*r.Network.Asn, 10)
			}
			if !containsAll(text, strings.Fields(q)) {
				continue
			}
			want = append(want, int32(i))
			brute.Ips += r.Ips
			countries[*r.Place.CountryCode] = true
			if r.Place.City != nil {
				cities[r.Place] = true
			}
			if r.Network.Asn != nil {
				asns[*r.Network.Asn] = true
			}
		}
		brute.Countries, brute.Cities, brute.Asns = len(countries), len(cities), len(asns)
		got := l.Match("city_asn", q, nil)
		if !reflect.DeepEqual(got, append([]int32{}, want...)) || l.Totals("city_asn", got) != brute {
			t.Fatalf("%q: %v %+v, want %v %+v", q, got, l.Totals("city_asn", got), want, brute)
		}
	}
}

func deref(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}

func containsAll(text string, words []string) bool {
	for _, w := range words {
		if !strings.Contains(text, w) {
			return false
		}
	}
	return true
}

// Readers of a loaded list run beside later loads that append names to the
// same tables (the first list leaves them spare capacity). Under -race this
// fails if a load writes memory an earlier list reads.
func TestNetListReadersBesideALoad(t *testing.T) {
	n, l := fakeList(t, testPlaces, testNetworks, []fake{{'y', 2, 7, 5, 1, 1, 1}, {'y', 3, 8, 9, 1, 1, 1}, {'y', 4, 9, 7, 1, 1, 1}})
	if cap(l.names.places) == len(l.names.places) {
		t.Fatal("the fixture leaves no spare capacity: a later load would not share the tables")
	}
	want := l.Totals("city_asn", l.Match("city_asn", "united", nil))
	var wg sync.WaitGroup
	for range 4 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for range 200 {
				got := l.Totals("city_asn", l.Match("city_asn", "united", l.ByIps("city_asn")))
				if r := l.Row("city_asn", 1); got != want || *r.Place.City != "London" {
					t.Errorf("totals %+v, row %+v", got, r)
					return
				}
			}
		}()
	}
	for i := range int32(200) {
		n.mu.Lock()
		from, fresh := [2]int{len(n.names.places), len(n.names.networks)}, &NetList{}
		s := &scannedRow{id: 2, code: 'y', place: 100 + i, network: 100 + i}
		s.Ips = 1
		if err := n.add(fresh, s); err != nil {
			t.Error(err)
		}
		*n.places[100+i] = Place{CountryCode: str("US"), Country: str("United States"), City: str("New")}
		*n.networks[100+i] = Network{Asn: num(int64(i)), Name: str("New Net")}
		n.fillTexts(from)
		fresh.finish(n.names)
		n.mu.Unlock()
	}
	wg.Wait()
}
