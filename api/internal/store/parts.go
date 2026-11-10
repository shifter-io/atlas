package store

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
)

// A build stored since calculated v8 (proxy-index calculated/parts.py,
// contract K1) keeps a small head in calculated_snapshots.payload. A v4 head
// (contract K9) is all the API reads of it: SnapshotRow.Payload is that head,
// and the network lists put its product groups back (Full, network.go).
//
// snapshotSQL reads the current build's head, when it is a v4 head, and
// whether it is: a head with networks and schema_version 4. It is read
// without its parts count and its per-cohort overlap and fleet rows: the
// api_head the build was published with (calculated v14, proxy-index
// calculated/api_head.sql), its text read as it is stored, else, for a build
// published before v14, built from the head (networkHeadSQL, network.go),
// the same value. A build with an api_head is a v4 build. An older build's
// payload is not read.
const snapshotSQL = `SELECT s.build_id, s.content_hash, CASE WHEN s.api_head IS NOT NULL THEN s.api_head WHEN n.v4 THEN ` + networkHeadSQL + ` END, n.v4
 FROM public.calculated_snapshots s JOIN public.current_calculation c ON c.build_id = s.build_id
 CROSS JOIN LATERAL (SELECT CASE WHEN s.api_head IS NOT NULL THEN true
  ELSE s.payload ? 'networks' AND s.payload->>'schema_version' IS NOT DISTINCT FROM '4' END AS v4) n WHERE c.singleton`

// productsKey is the key the head leaves out and withProducts puts back.
const productsKey = "products"

// withProducts is the head (jsonb text, without "parts") with a "products"
// array put where jsonb orders that key; groups writes the array's elements.
func withProducts(head []byte, groups func(*bytes.Buffer) error) ([]byte, error) {
	fields, err := objectFields(head)
	if err != nil {
		return nil, fmt.Errorf("head: %w", err)
	}
	// After the keys jsonb sorts before products (the head's first ones).
	at, before := 1, 0
	for _, f := range fields {
		if f.Key == productsKey {
			return nil, fmt.Errorf("head holds %s", productsKey)
		}
		if jsonbBefore(f.Key, productsKey) {
			at, before = f.End, before+1
		}
	}
	var out bytes.Buffer
	out.Write(head[:at])
	if before > 0 {
		out.WriteString(", ")
	}
	out.WriteString(`"` + productsKey + `": [`)
	if err := groups(&out); err != nil {
		return nil, err
	}
	out.WriteByte(']')
	if before == 0 && len(fields) > 0 {
		out.WriteString(", ")
	}
	out.Write(head[at:])
	return out.Bytes(), nil
}

// jsonbBefore reports whether jsonb orders key a before key b: shorter keys
// first, then bytewise.
func jsonbBefore(a, b string) bool {
	return len(a) < len(b) || len(a) == len(b) && a < b
}

// field is one member of a JSON object: its key and the bytes of its value,
// raw[Start:End].
type field struct {
	Key        string
	Start, End int
}

// objectFields lists the members of a JSON object in their order, without
// decoding their values.
func objectFields(raw []byte) ([]field, error) {
	dec := json.NewDecoder(bytes.NewReader(raw))
	if t, err := dec.Token(); err != nil || t != json.Delim('{') {
		return nil, errors.New("not a JSON object")
	}
	out := []field{}
	for dec.More() {
		t, err := dec.Token()
		if err != nil {
			return nil, err
		}
		var v json.RawMessage
		if err := dec.Decode(&v); err != nil {
			return nil, err
		}
		end := int(dec.InputOffset())
		out = append(out, field{Key: t.(string), Start: end - len(v), End: end})
	}
	return out, nil
}

// member is the member named key.
func member(fields []field, key string) (field, bool) {
	for _, f := range fields {
		if f.Key == key {
			return f, true
		}
	}
	return field{}, false
}

// valueOf is the raw value of key, nil when the object has none.
func valueOf(raw []byte, fields []field, key string) json.RawMessage {
	if f, ok := member(fields, key); ok {
		return raw[f.Start:f.End]
	}
	return nil
}

// splice is raw with the value of f replaced by v, in a new slice.
func splice(raw []byte, f field, v []byte) []byte {
	out := make([]byte, 0, len(raw)-(f.End-f.Start)+len(v))
	return append(append(append(out, raw[:f.Start]...), v...), raw[f.End:]...)
}
