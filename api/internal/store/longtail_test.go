package store

import (
	"context"
	"encoding/json"
	"testing"
)

func cappedPayload(t *testing.T) []byte {
	t.Helper()
	window := func(hours int, cities, asns string) map[string]any {
		return map[string]any{
			"period":            map[string]any{"hours": hours},
			"breakdowns":        map[string]any{"country": []any{"DE"}, "city_asn": json.RawMessage(cities)},
			"breakdown_entries": map[string]any{"country": 1, "city_asn": 3},
			"pool": map[string]any{
				"unique_ips": 9, "asns_breakdown": json.RawMessage(asns),
				"list_entries": map[string]any{"asns_breakdown": 2},
			},
		}
	}
	b, err := json.Marshal(map[string]any{"schema_version": 2, "products": []any{map[string]any{
		"product_id": 1, "cohort": "c", "name": "kept",
		"windows": map[string]any{"24h": window(24, `["a"]`, `["x"]`), "30d": window(720, `["b"]`, `["y","z"]`)},
	}}})
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func windows(t *testing.T, payload []byte) map[string]map[string]map[string]any {
	t.Helper()
	var doc struct {
		Products []struct {
			Name    string                               `json:"name"`
			Windows map[string]map[string]map[string]any `json:"windows"`
		} `json:"products"`
	}
	if err := json.Unmarshal(payload, &doc); err != nil {
		t.Fatal(err)
	}
	if doc.Products[0].Name != "kept" {
		t.Fatalf("product fields changed: %s", payload)
	}
	return doc.Products[0].Windows
}

func TestRestoreLongTailPutsBackOnlyCappedListsWithMatchingCounts(t *testing.T) {
	tails := map[tailKey]longTail{{1, "c", 24}: {
		cityAsnCount: 3, cityAsn: json.RawMessage(`["a","b","c"]`),
		asnsCount: 2, asns: json.RawMessage(`["x","w"]`),
	}, {1, "c", 720}: {
		cityAsnCount: 3, cityAsn: json.RawMessage(`["b","c","d"]`),
		asnsCount: 2, asns: json.RawMessage(`["y","z"]`),
	}}
	full, err := restoreLongTail(cappedPayload(t), tails)
	if err != nil {
		t.Fatal(err)
	}
	w := windows(t, full)
	day, month := w["24h"], w["30d"]
	if got, _ := json.Marshal(day["breakdowns"]["city_asn"]); string(got) != `["a","b","c"]` {
		t.Errorf("24h city_asn %s", got)
	}
	if got, _ := json.Marshal(day["pool"]["asns_breakdown"]); string(got) != `["x","w"]` {
		t.Errorf("24h asns %s", got)
	}
	// The 30d ASN list was already whole: it stays as published.
	if got, _ := json.Marshal(month["pool"]["asns_breakdown"]); string(got) != `["y","z"]` {
		t.Errorf("30d asns %s", got)
	}
	if got, _ := json.Marshal(month["breakdowns"]["country"]); string(got) != `["DE"]` || month["pool"]["unique_ips"] != 9.0 {
		t.Errorf("30d other fields changed: %v", month)
	}
}

func TestRestoreLongTailLeavesThePayloadWhenRowsDoNotMatch(t *testing.T) {
	for name, tails := range map[string]map[tailKey]longTail{
		"no typed rows":    {},
		"other cohort":     {{1, "d", 24}: {cityAsnCount: 3, cityAsn: json.RawMessage(`[1,2,3]`)}},
		"count mismatch":   {{1, "c", 24}: {cityAsnCount: 4, cityAsn: json.RawMessage(`[1,2,3,4]`)}},
		"already restored": {{1, "c", 720}: {asnsCount: 2, asns: json.RawMessage(`[1,2]`)}},
	} {
		full, err := restoreLongTail(cappedPayload(t), tails)
		if err != nil || full != nil {
			t.Errorf("%s: restored %s, %v", name, full, err)
		}
	}
	if _, err := restoreLongTail([]byte(`{"products":{}}`), nil); err == nil {
		t.Error("a malformed snapshot was accepted")
	}
	bad := []byte(`{"products":[{"product_id":1,"cohort":"c","windows":{"24h":{"period":{"hours":24},"pool":{},` +
		`"breakdowns":{"city_asn":[]},"breakdown_entries":{"city_asn":"many"}}}}]}`)
	if _, err := restoreLongTail(bad, map[tailKey]longTail{{1, "c", 24}: {}}); err == nil {
		t.Error("malformed entry counts were accepted")
	}
}

func TestSnapshotsWithoutEntryCountsAreServedAsPublished(t *testing.T) {
	s := &SnapshotRow{Payload: []byte(`{"schema_version":2,"products":[]}`)}
	// An uncapped snapshot never queries the typed rows (no transaction is needed).
	if err := s.readLongTail(context.Background(), nil); err != nil {
		t.Fatal(err)
	}
	if string(s.Full()) != string(s.Payload) {
		t.Errorf("Full %s", s.Full())
	}
	s.full = []byte(`{}`)
	if string(s.Full()) != `{}` {
		t.Errorf("Full ignores the restored snapshot: %s", s.Full())
	}
}
