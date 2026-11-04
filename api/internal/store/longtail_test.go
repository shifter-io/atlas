package store

import (
	"context"
	"encoding/json"
	"errors"
	"slices"
	"strings"
	"testing"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

func str(s string) *string { return &s }

// testList is a list with three city x ASN rows and three ASN rows (one
// without IPs), as lists.go reads them.
func testList() *List {
	de := &Place{CountryCode: str("DE"), Country: str("Germany"), RegionCode: str("BE"), Region: str("Berlin"), City: str("Berlin")}
	net := func(asn int64, name string) *Network {
		return &Network{Asn: &asn, Name: str(name), UsageType: str("isp")}
	}
	row := func(p *Place, n *Network, ips, completed, successes int64) ListRow {
		return ListRow{Place: p, Network: n, Ips: ips, Observations: ips * 2, Completed: completed, Successes: successes, Challenges: 0, Blocks: 0, P50: 40, P90: -1, P99: -1}
	}
	return NewList(map[string][]ListRow{
		"city_asn": {row(de, net(3320, "DTAG"), 5, 10, 10), row(de, net(3209, "Voda"), 4, 3, 1), row(de, net(6805, `O"2`), 1, 0, 0)},
		"asn":      {row(nil, net(6805, "Telefonica"), 7, 1, 1), row(nil, net(3320, "DTAG"), 7, 1, 1), row(nil, net(1, "Idle"), 0, 1, 0)},
	})
}

// v2Window is a slimmed v2 window as jsonb writes it: one of three city x ASN
// rows and one of two ASN rows kept.
const v2Window = `{"pool": {"unique_ips": 9, "list_entries": {"asns_breakdown": 2}, "asns_breakdown": ["x"]}, "period": {"hours": 24}, ` +
	`"breakdowns": {"city_asn": ["a"]}, "breakdown_entries": {"city_asn": 3}}`

func groupPayload(window string) []byte {
	return []byte(`{"products": [{"kind": "residential", "name": "kept", "cohort": "c", "windows": {"24h": ` + window +
		`}, "provider": "p", "product_id": 1}], "schema_version": 2}`)
}

func restored(t *testing.T, payload []byte, l *List, picked bool) (map[string]any, []byte) {
	t.Helper()
	s := &SnapshotRow{Payload: payload, Places: map[PlaceWindow]*List{{1, "c", 24}: l}}
	pf, items, groups, err := s.groups()
	if err != nil {
		t.Fatal(err)
	}
	if err := s.restore(pf, items, groups, map[groupKey]bool{{1, "c"}: picked}); err != nil {
		t.Fatal(err)
	}
	var doc struct {
		Products []struct {
			Name    string                    `json:"name"`
			Windows map[string]map[string]any `json:"windows"`
		} `json:"products"`
	}
	if err := json.Unmarshal(s.Full(), &doc); err != nil {
		t.Fatalf("%v\n%s", err, s.Full())
	}
	if doc.Products[0].Name != "kept" {
		t.Fatalf("product fields changed: %s", s.Full())
	}
	return doc.Products[0].Windows["24h"], s.Full()
}

func marshal(v any) string { b, _ := json.Marshal(v); return string(b) }

func TestRestoreV2PutsBackTheWholeListsWithMatchingCounts(t *testing.T) {
	w, full := restored(t, groupPayload(v2Window), testList(), true)
	cities := w["breakdowns"].(map[string]any)["city_asn"].([]any)
	if len(cities) != 3 || cities[2].(map[string]any)["name"] != `O"2` || cities[2].(map[string]any)["success_rate"] != nil {
		t.Errorf("city_asn %v", cities)
	}
	// ASNs with IPs, most first; the tie in Python's str() order ('asn': 3320 < 'asn': 6805).
	if got := marshal(w["pool"].(map[string]any)["asns_breakdown"]); got != `[{"asn":3320,"ips":7,"name":"DTAG","observations":14,"usage_type":"isp"},{"asn":6805,"ips":7,"name":"Telefonica","observations":14,"usage_type":"isp"}]` {
		t.Errorf("asns %s", got)
	}
	// Rows as jsonb writes them: keys shorter first, ", " and ": ".
	if !strings.Contains(string(full), `{"asn": 3320, "ips": 5, "p50": 40, "p90": null, "p99": null, "city": "Berlin", "name": "DTAG", "blocks": 0, "region": "Berlin", `+
		`"country": "Germany", "completed": 10, "successes": 10, "challenges": 0, "usage_type": "isp", "region_code": "BE", "country_code": "DE", "observations": 10, "success_rate": 1.0}`) {
		t.Errorf("row text %s", full)
	}
	if w["pool"].(map[string]any)["unique_ips"] != 9.0 {
		t.Errorf("pool fields changed %v", w["pool"])
	}
	// Not the provider's group: city x ASN stays as published.
	w, _ = restored(t, groupPayload(v2Window), testList(), false)
	if got := marshal(w["breakdowns"]); got != `{"city_asn":["a"]}` {
		t.Errorf("unpicked city_asn %s", got)
	}
}

func TestRestoreV2LeavesListsWhoseRowsDoNotMatch(t *testing.T) {
	l := testList()
	l.rows['y'] = l.rows['y'][:2]
	l.rows['a'] = l.rows['a'][:1]
	_, full := restored(t, groupPayload(v2Window), l, true)
	if string(full) != string(groupPayload(v2Window)) {
		t.Errorf("restored %s", full)
	}
}

func TestRestoreV3IsItsV2Form(t *testing.T) {
	v3 := `{"pool": {"unique_ips": 9, "list_entries": {"asns_breakdown": 2}}, "lists": {"id": 7, "entries": {"asn": 1, "city_asn": 1, "asns_breakdown": 1}}, ` +
		`"period": {"hours": 24}, "breakdown_entries": {"city_asn": 3}}`
	w, _ := restored(t, groupPayload(v3), testList(), true)
	if _, ok := w["lists"]; ok {
		t.Error("lists kept")
	}
	bd := w["breakdowns"].(map[string]any)
	if len(bd["city_asn"].([]any)) != 3 || len(bd["asn"].([]any)) != 1 || len(bd) != 2 {
		t.Errorf("breakdowns %v", bd)
	}
	if n := len(w["pool"].(map[string]any)["asns_breakdown"].([]any)); n != 2 {
		t.Errorf("asns %d rows, want the whole list", n)
	}
	// Another group's window has no city x ASN list.
	w, _ = restored(t, groupPayload(v3), testList(), false)
	if _, ok := w["breakdowns"].(map[string]any)["city_asn"]; ok {
		t.Error("city_asn of an unpicked v3 window")
	}
	bad := strings.Replace(v3, `"asn": 1,`, `"asn": 9,`, 1)
	s := &SnapshotRow{Payload: groupPayload(bad), Places: map[PlaceWindow]*List{{1, "c", 24}: testList()}}
	pf, items, groups, _ := s.groups()
	if err := s.restore(pf, items, groups, nil); err == nil {
		t.Error("a window keeping more rows than its list has was accepted")
	}
}

func TestJSONMembersAndPythonText(t *testing.T) {
	obj := []byte(`{"a": 1, "ccc": 3}`)
	for _, c := range []struct{ key, want string }{{"bb", `{"a": 1, "bb": 0, "ccc": 3}`}, {"", `{"": 0, "a": 1, "ccc": 3}`}, {"dddd", `{"a": 1, "ccc": 3, "dddd": 0}`}, {"a", `{"a": 0, "ccc": 3}`}} {
		if got, err := setMember(obj, c.key, []byte("0")); err != nil || string(got) != c.want {
			t.Errorf("set %q: %s %v", c.key, got, err)
		}
	}
	for key, want := range map[string]string{"a": `{"ccc": 3}`, "ccc": `{"a": 1}`, "x": `{"a": 1, "ccc": 3}`} {
		if got, _ := dropMember(obj, key); string(got) != want {
			t.Errorf("drop %q: %s", key, got)
		}
	}
	if got, _ := dropMember([]byte(`{"a": 1}`), "a"); string(got) != `{}` {
		t.Errorf("drop only member: %s", got)
	}
	if got, _ := setMember([]byte(`{}`), "a", []byte("1")); string(got) != `{"a": 1}` {
		t.Errorf("set into empty: %s", got)
	}
	for s, want := range map[string]string{"it's": `"it's"`, `a"b`: `'a"b'`, `a'b"c`: `'a\'b"c'`, "x\ty\x01\u00a0\u00e9\u200b\U0001f600\U000e0001": "'x\\ty\\x01\\xa0\u00e9\\u200b\U0001f600\\U000e0001'"} {
		if got := pyStr(s); got != want {
			t.Errorf("pyStr(%q) = %s, want %s", s, got, want)
		}
	}
	if got := string(appendString(nil, "a\"\\\n\x01é<")); got != `"a\"\\\n\u0001é<"` {
		t.Errorf("appendString %s", got)
	}
	for c, want := range map[[2]int64]string{{1, 1}: "1.0", {19, 20}: "0.95", {1, 100000}: "0.00001", {0, 0}: "null", {-1, 5}: "null"} {
		r := ListRow{Successes: c[0], Completed: c[1]}
		if got := string(writers["success_rate"](nil, &r)); got != want {
			t.Errorf("success_rate %v = %s, want %s", c, got, want)
		}
	}
}

func TestSnapshotsNeverSlimmedAreServedAsPublished(t *testing.T) {
	s := &SnapshotRow{Payload: []byte(`{"schema_version":2,"products":[]}`)}
	// A snapshot without list counts never queries the typed rows (no transaction is needed).
	if err := s.readLists(context.Background(), nil, nil); err != nil {
		t.Fatal(err)
	}
	if string(s.Full()) != string(s.Payload) || s.Places != nil {
		t.Errorf("Full %s", s.Full())
	}
}

func TestMalformedSnapshotsAreErrors(t *testing.T) {
	for _, payload := range []string{`not json`, `{"products": {}}`, `{"products": [1]}`, `{"products": [`} {
		s := &SnapshotRow{Payload: []byte(payload)}
		if _, _, _, err := s.groups(); err == nil {
			t.Errorf("%s: accepted", payload)
		}
	}
	for _, window := range []string{`[]`, `{"pool": []}`, `{"pool": {}, "breakdown_entries": {"city_asn": "many"}}`,
		`{"pool": {}, "lists": {"entries": {"asn": 9}}}`} {
		if _, err := restoreWindow([]byte(window), testList(), true); err == nil {
			t.Errorf("%s: accepted", window)
		}
	}
	for _, f := range []func() error{
		func() error { _, err := setMember([]byte(`[`), "a", nil); return err },
		func() error { _, err := dropMember([]byte(`{"a": `), "a"); return err },
		func() error { _, err := arrayItems([]byte(`{}`)); return err },
		func() error { _, err := arrayItems([]byte(`[1, }`)); return err },
	} {
		if f() == nil {
			t.Error("malformed JSON accepted")
		}
	}
	if length([]byte(`[`), "a") != 0 {
		t.Error("length of a malformed object")
	}
	s := &SnapshotRow{Payload: []byte(`{"products": [{"product_id": 1, "cohort": "c"}, {"product_id": 1, "cohort": "c", "windows": {"24h": {}}}]}`),
		Places: map[PlaceWindow]*List{}}
	pf, items, groups, _ := s.groups()
	if err := s.restore(pf, items, groups, nil); err != nil || s.full != nil {
		t.Errorf("groups without lists changed: %s %v", s.full, err)
	}
}

func TestV3WindowWithoutAPoolAndRowsWithoutLabels(t *testing.T) {
	w, _ := restored(t, groupPayload(`{"lists": {"id": 1, "entries": {"geo": 0}}, "period": {"hours": 24}}`), testList(), true)
	if got := marshal(w["pool"]); got != `{"geo":[]}` {
		t.Errorf("pool %s", got)
	}
	bare := ListRow{Ips: 1, Observations: 1, Completed: -1, Successes: -1, P50: -1, P90: -1, P99: -1}
	if got := string(rowsJSON(rowColumns['y'], []*ListRow{&bare})); !strings.Contains(got, `"asn": null`) || !strings.Contains(got, `"city": null`) {
		t.Errorf("row without labels %s", got)
	}
	if got := pyRepr(poolKeys["asns_breakdown"], &bare); got != "{'asn': None, 'name': None, 'usage_type': None, 'ips': 1, 'observations': 1}" {
		t.Errorf("repr %s", got)
	}
	g := []listGroup{{Provider: "p", ProductID: 1, Cohort: "a", Windows: map[string]groupWindow{"30d": {}}}, {Provider: "p", ProductID: 2, Cohort: "b"}}
	if got := headlineGroups(g); !got[groupKey{1, "a"}] || len(got) != 1 {
		t.Errorf("headline groups %v", got)
	}
}

// observedTx answers every query with no rows, recording the list ids the
// cache holds when the first one runs.
type observedTx struct {
	pgx.Tx
	c       *Lists
	held    []int64
	cityAsn bool
}

type noRows struct{ pgx.Rows }

func (noRows) Next() bool                    { return false }
func (noRows) Err() error                    { return nil }
func (noRows) Close()                        {}
func (noRows) CommandTag() pgconn.CommandTag { return pgconn.CommandTag{} }

func (o *observedTx) Query(context.Context, string, ...any) (pgx.Rows, error) {
	if o.held == nil {
		o.held = []int64{}
		for id, l := range o.c.held {
			o.held = append(o.held, id)
			_, y := l.rows[cityAsn[0]]
			o.cityAsn = o.cityAsn || y
		}
		slices.Sort(o.held)
	}
	return noRows{}, nil
}

// B2: a rebuild drops the lists the new build does not reference (and the
// city x ASN rows it no longer reads) before it reads new rows, so two
// builds' lists are never held at once.
func TestFetchDropsUnreferencedListsBeforeReading(t *testing.T) {
	c := NewLists()
	c.k2, c.held, c.places, c.networks = true, map[int64]*List{1: testList(), 2: testList(), 3: testList()}, map[int32]*Place{}, map[int32]*Network{}
	tx := &observedTx{c: c}
	got, err := c.fetch(context.Background(), tx, true, 0, []int64{3, 4}, map[int64]bool{})
	if err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(tx.held, []int64{3}) || tx.cityAsn {
		t.Errorf("held %v (city x ASN rows %v) while reading, want only list 3 without its city x ASN rows", tx.held, tx.cityAsn)
	}
	if len(got) != 2 || len(got[3].Rows("asn")) != 3 || got[3].Rows("city_asn") != nil || got[4].Rows("asn") == nil {
		t.Errorf("fetched %v", got)
	}
}

// A failed fetch keeps no list and no pending label.
func TestAFailedFetchKeepsNothing(t *testing.T) {
	c := NewLists()
	c.k2, c.held, c.places, c.networks = true, map[int64]*List{3: testList()}, map[int32]*Place{}, map[int32]*Network{}
	if _, err := c.fetch(context.Background(), failingTx{}, true, 0, []int64{3, 4}, nil); err == nil {
		t.Fatal("a failed read was not an error")
	}
	if c.held != nil || c.missing != nil {
		t.Errorf("a failed fetch kept %v, pending %v", c.held, c.missing)
	}
}

type failingTx struct{ pgx.Tx }

func (failingTx) Query(context.Context, string, ...any) (pgx.Rows, error) {
	return nil, errors.New("connection lost")
}

type failingBeginner struct{}

func (failingBeginner) BeginTx(context.Context, pgx.TxOptions) (pgx.Tx, error) {
	return nil, errors.New("connection refused")
}

func TestLoadListsReportsAnUnreachableDatabase(t *testing.T) {
	if _, err := LoadLists(context.Background(), failingBeginner{}, NewLists()); err == nil || !strings.Contains(err.Error(), "connection refused") {
		t.Errorf("load error %v", err)
	}
}
