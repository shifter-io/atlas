package store

import (
	"bytes"
	"errors"
	"strings"
	"testing"
)

func TestObjectFieldsSpanTheRawValues(t *testing.T) {
	t.Parallel()
	raw := []byte(` { "a" : [1, {"x": 2}] ,"b\"c":"d",  "e": null } `)
	fields, err := objectFields(raw)
	if err != nil {
		t.Fatal(err)
	}
	got := []string{}
	for _, f := range fields {
		got = append(got, f.Key+"="+string(raw[f.Start:f.End]))
	}
	if strings.Join(got, "|") != `a=[1, {"x": 2}]|b"c="d"|e=null` {
		t.Errorf("%q", got)
	}
	for _, bad := range []string{``, `[1]`, `{"a": }`, `{"a": 1`, `{1: 2}`} {
		if _, err := objectFields([]byte(bad)); err == nil {
			t.Errorf("%q: no error", bad)
		}
	}
}

func TestWithProductsPutsTheGroupsWhereJsonbOrdersThem(t *testing.T) {
	t.Parallel()
	two := func(out *bytes.Buffer) error { out.WriteString(`{"id": 1}, {"id": 2}`); return nil }
	none := func(*bytes.Buffer) error { return nil }
	for head, want := range map[string]string{
		`{}`:                 `{"products": [{"id": 1}, {"id": 2}]}`,
		`{"as_of": "t"}`:     `{"as_of": "t", "products": [{"id": 1}, {"id": 2}]}`,
		`{"overlap": []}`:    `{"overlap": [], "products": [{"id": 1}, {"id": 2}]}`,
		`{"protocol": 1}`:    `{"products": [{"id": 1}, {"id": 2}], "protocol": 1}`,
		`{"readiness": "r"}`: `{"products": [{"id": 1}, {"id": 2}], "readiness": "r"}`,
		`{"fleet": [], "produces": 1, "protocol": 2, "snapshot_id": "s"}`: `{"fleet": [], "produces": 1, "products": [{"id": 1}, {"id": 2}], "protocol": 2, "snapshot_id": "s"}`,
	} {
		got, err := withProducts([]byte(head), two)
		if err != nil || string(got) != want {
			t.Errorf("%s: %s %v", head, got, err)
		}
	}
	if got, err := withProducts([]byte(`{"as_of": "t"}`), none); err != nil || string(got) != `{"as_of": "t", "products": []}` {
		t.Errorf("no groups: %s %v", got, err)
	}
	if _, err := withProducts([]byte(`{"products": []}`), none); err == nil || !strings.Contains(err.Error(), "head holds products") {
		t.Errorf("head with products: %v", err)
	}
	if _, err := withProducts([]byte(`[]`), none); err == nil {
		t.Error("head not an object: no error")
	}
	stop := errors.New("stop")
	if _, err := withProducts([]byte(`{}`), func(*bytes.Buffer) error { return stop }); !errors.Is(err, stop) {
		t.Errorf("groups error: %v", err)
	}
}

func TestJsonbOrder(t *testing.T) {
	t.Parallel()
	for _, c := range [][2]string{{"runs", "parts"}, {"parts", "products"}, {"produces", "products"}, {"products", "protocol"}, {"products", "readiness"}} {
		if !jsonbBefore(c[0], c[1]) || jsonbBefore(c[1], c[0]) {
			t.Errorf("%s before %s", c[0], c[1])
		}
	}
	if jsonbBefore("products", "products") {
		t.Error("a key before itself")
	}
}
