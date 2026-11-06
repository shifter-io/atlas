//go:build !race

package store

import (
	"fmt"
	"runtime"
	"testing"
)

// netListBound is the most heap a million network list rows may take with
// their names (K11: about 24 bytes a row, at most 11 MB per network).
const netListBound = 40 << 20

// A million compact rows over 50,000 places and 5,000 ASN labels, as Load
// holds them, take at most netListBound once collected. The race detector
// (excluded by the build tag) multiplies heap use.
// Serial: it measures the process's heap, which parallel tests would share.
func TestNetListsMemory(t *testing.T) {
	if testing.Short() {
		t.Skip("holds a million rows")
	}
	places, networks := map[int32]Place{}, map[int32]Network{}
	for i := range int32(50_000) {
		places[i] = Place{CountryCode: str("XX"), Country: str("Country"), Region: str(fmt.Sprint("Region ", i%900)), City: str(fmt.Sprint("City ", i))}
	}
	for i := range int32(5_000) {
		networks[i] = Network{Asn: num(int64(i)), Name: str(fmt.Sprint("Network ", i)), UsageType: str("isp")}
	}
	rows := make([]fake, 1_000_000)
	for i := range rows {
		rows[i] = fake{'y', int32(i % 50_000), int32(i % 5_000), int64(i % 997), int64(i), int64(i / 2), int32(i % 3000)}
	}
	var before, after runtime.MemStats
	runtime.GC()
	runtime.ReadMemStats(&before)
	n, l := fakeList(t, places, networks, rows)
	runtime.GC()
	runtime.ReadMemStats(&after)
	held := int64(after.HeapAlloc) - int64(before.HeapAlloc)
	t.Logf("1,000,000 rows hold %.1f MB (%.1f bytes a row)", float64(held)/(1<<20), float64(held)/1e6)
	if held > netListBound {
		t.Errorf("1,000,000 rows hold %d bytes, more than %d", held, netListBound)
	}
	runtime.KeepAlive(n)
	runtime.KeepAlive(l)
	runtime.KeepAlive(rows)
	runtime.KeepAlive(places)
	runtime.KeepAlive(networks)
}
