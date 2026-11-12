package store

import (
	"math"
	"strings"
	"testing"
)

// A compact row keeps a scanned row's IP rank and peers (calculated v15) and
// gives them back; NULL (0) stays 0, and a rank past the row fails the load.
func TestNetRowKeepsIpsRank(t *testing.T) {
	t.Parallel()
	n := NewNetLists(nil)
	s := &scannedRow{id: 5, code: 'y', place: -1, network: -1, ipsRank: 3, ipsPeers: 18}
	s.Ips = 100
	c, err := n.compact(s)
	if err != nil {
		t.Fatal(err)
	}
	s.ipsRank, s.ipsPeers = 0, 0
	none, err := n.compact(s)
	if err != nil {
		t.Fatal(err)
	}
	rows := &NetRows{rows: []netRow{c, none}}
	if r := rows.Row(0); r.IpsRank != 3 || r.IpsPeers != 18 {
		t.Errorf("rank %d of %d, want 3 of 18", r.IpsRank, r.IpsPeers)
	}
	if r := rows.Row(1); r.IpsRank != 0 || r.IpsPeers != 0 {
		t.Errorf("NULL rank %d of %d, want 0 of 0", r.IpsRank, r.IpsPeers)
	}
	s.ipsRank, s.ipsPeers = 1, math.MaxUint16+1
	if _, err := n.compact(s); err == nil || !strings.Contains(err.Error(), "does not fit") {
		t.Errorf("peers past the row: %v", err)
	}
	if _, err := NewNetList(map[string][]NetRow{"city_asn": {{Ips: 1, IpsRank: -1, IpsPeers: 1}}}); err == nil {
		t.Error("a negative rank was kept")
	}
	l, err := NewNetList(map[string][]NetRow{"city_asn": {{Ips: 500, IpsRank: 1, IpsPeers: 3}}})
	if err != nil {
		t.Fatal(err)
	}
	if r := (&NetRows{rows: l.rows[strings.IndexByte(netCodes, 'y')], names: l.names}).Row(0); r.IpsRank != 1 || r.IpsPeers != 3 {
		t.Errorf("NewNetList row: rank %d of %d, want 1 of 3", r.IpsRank, r.IpsPeers)
	}
}
