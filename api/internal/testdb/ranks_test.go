package testdb

import (
	"context"
	"fmt"
	"testing"

	"github.com/Zeus-Development/atlas/api/internal/store"
	"github.com/Zeus-Development/atlas/api/internal/testscale"
	"github.com/jackc/pgx/v5"
)

// The fixture's ranks (netRanks, calculated v15's rule) as the store reads
// them back: three networks with 500, 500 and 100 IPs at one City x ASN rank
// 1, 1 and 3 of 3; a place one network alone has is 1 of 1; another kind
// and a row without IPs rank apart (none for the latter).
func TestNetListIpsRanks(t *testing.T) {
	db := New(t)
	ctx := context.Background()
	c := db.Conn(t)
	defer c.Close(ctx)
	la := place("Los Angeles")
	comcast := &store.Network{Asn: ptr(int64(7922)), Name: ptr("Comcast"), UsageType: ptr("residential")}
	at := func(p *store.Place, ips int64) store.NetRow {
		return store.NetRow{Place: p, Network: comcast, Ips: ips, Completed: -1, Successes: -1, P50: -1}
	}
	ranks := netRanks{kinds: map[[2]string]string{}}
	write := func(provider, kind string, rows ...store.NetRow) int64 {
		ranks.kinds[[2]string{provider, "p"}] = kind
		id, _, err := writeNetList(ctx, c, 1, Product(t, c, provider, "p"), 24, map[string][]store.NetRow{"city_asn": rows})
		if err != nil {
			t.Fatal(err)
		}
		ranks.add(id, testscale.NetWindow{Provider: provider, Product: "p"})
		return id
	}
	a := write("alice", "residential", at(la, 500), at(place("San Diego"), 40))
	b := write("bob", "residential", at(la, 500), at(place("Fresno"), 0))
	carol := write("carol", "residential", at(la, 100))
	dc := write("dave", "datacenter", at(la, 900))
	if err := ranks.write(ctx, c); err != nil {
		t.Fatal(err)
	}
	if err := countRows(ctx, c, true); err != nil {
		t.Fatal(err)
	}
	want := map[int64][][2]int32{a: {{1, 3}, {1, 1}}, b: {{1, 3}, {0, 0}}, carol: {{3, 3}}, dc: {{1, 1}}}
	got := readRanks(t, c, a, b, carol, dc)
	if len(got) != len(want) {
		t.Fatalf("read %d lists, want %d", len(got), len(want))
	}
	for id, rows := range got {
		if s := fmt.Sprint(rows); s != fmt.Sprint(want[id]) {
			t.Errorf("list %d: ranks %s, want %v", id, s, want[id])
		}
	}
}

// readRanks is every list's City x ASN [rank, peers], in position order, as
// store.NetLists reads them.
func readRanks(t *testing.T, c *pgx.Conn, ids ...int64) map[int64][][2]int32 {
	t.Helper()
	ctx := context.Background()
	n := store.NewNetLists(c)
	tx, err := c.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	lists, err := n.Load(ctx, tx, ids)
	_ = tx.Rollback(ctx)
	if err != nil {
		t.Fatal(err)
	}
	out := map[int64][][2]int32{}
	for id, l := range lists {
		rows, err := l.Rows(ctx, "city_asn")
		if err != nil {
			t.Fatal(err)
		}
		for i := range rows.Len() {
			r := rows.Row(i)
			out[id] = append(out[id], [2]int32{r.IpsRank, r.IpsPeers})
		}
	}
	return out
}

func place(city string) *store.Place {
	return &store.Place{CountryCode: ptr("US"), Country: ptr("United States"), RegionCode: ptr("CA"), Region: ptr("California"), City: ptr(city)}
}

func ptr[T any](v T) *T { return &v }
