package store

import (
	"context"
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

func TestJSONMembersAndPythonText(t *testing.T) {
	t.Parallel()
	obj := []byte(`{"a": 1, "ccc": 3}`)
	for _, c := range []struct{ key, want string }{{"bb", `{"a": 1, "bb": 0, "ccc": 3}`}, {"", `{"": 0, "a": 1, "ccc": 3}`}, {"dddd", `{"a": 1, "ccc": 3, "dddd": 0}`}, {"a", `{"a": 0, "ccc": 3}`}} {
		if got, err := setMember(obj, c.key, []byte("0")); err != nil || string(got) != c.want {
			t.Errorf("set %q: %s %v", c.key, got, err)
		}
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

func TestMalformedJSONIsAnError(t *testing.T) {
	t.Parallel()
	for _, f := range []func() error{
		func() error { _, err := setMember([]byte(`[`), "a", nil); return err },
		func() error { _, err := arrayItems([]byte(`{}`)); return err },
		func() error { _, err := arrayItems([]byte(`[1, }`)); return err },
	} {
		if f() == nil {
			t.Error("malformed JSON accepted")
		}
	}
}

func TestRowsWithoutLabels(t *testing.T) {
	t.Parallel()
	bare := ListRow{Ips: 1, Observations: 1, Completed: -1, Successes: -1, P50: -1, P90: -1, P99: -1}
	if got := string(rowsJSON(rowColumns['y'], []*ListRow{&bare})); !strings.Contains(got, `"asn": null`) || !strings.Contains(got, `"city": null`) {
		t.Errorf("row without labels %s", got)
	}
	if got := pyRepr(poolKeys["asns_breakdown"], &bare); got != "{'asn': None, 'name': None, 'usage_type': None, 'ips': 1, 'observations': 1}" {
		t.Errorf("repr %s", got)
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
	t.Parallel()
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
	t.Parallel()
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
	t.Parallel()
	if _, err := LoadLists(context.Background(), failingBeginner{}, NewLists()); err == nil || !strings.Contains(err.Error(), "connection refused") {
		t.Errorf("load error %v", err)
	}
}
