package store

import (
	"bytes"
	"encoding/json"
	"fmt"
)

// A v4 window's lists are put back into the payload the build decodes
// (network.go) with these helpers, as jsonb writes the published payload.

// Full is the snapshot with its lists put back: the payload itself when
// nothing was capped.
func (s *SnapshotRow) Full() []byte {
	if s.full != nil {
		return s.full
	}
	return s.Payload
}

// setMember is the object obj (jsonb text) with key's value set to v:
// replaced where it is, else added where jsonb orders the key.
func setMember(obj []byte, key string, v []byte) ([]byte, error) {
	fields, err := objectFields(obj)
	if err != nil {
		return nil, err
	}
	if f, ok := member(fields, key); ok {
		return splice(obj, f, v), nil
	}
	at, before := 1, false
	for _, f := range fields {
		if jsonbBefore(f.Key, key) {
			at, before = f.End, true
		}
	}
	add := appendString(nil, key)
	add = append(append(add, ": "...), v...)
	switch {
	case before:
		add = append([]byte(", "), add...)
	case len(fields) > 0:
		add = append(add, ", "...)
	}
	return splice(obj, field{Start: at, End: at}, add), nil
}

// arrayItems lists the elements of a JSON array, without decoding them.
func arrayItems(raw []byte) ([]field, error) {
	if len(raw) == 0 {
		return nil, nil
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	if t, err := dec.Token(); err != nil || t != json.Delim('[') {
		return nil, fmt.Errorf("not a JSON array")
	}
	out := []field{}
	for dec.More() {
		var v json.RawMessage
		if err := dec.Decode(&v); err != nil {
			return nil, err
		}
		end := int(dec.InputOffset())
		out = append(out, field{Start: end - len(v), End: end})
	}
	return out, nil
}
