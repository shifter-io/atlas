package jsnum

import (
	"encoding/json"
	"math"
	"testing"
)

// Expected values were produced by node (Number.prototype.toFixed,
// toLocaleString("en-US"), JSON.stringify, Math.round).
var cases = []struct {
	X          float64
	F0, F1, F2 string
	F3, Loc    string
	JSON       string
	Round      float64
}{
	{0, "0", "0.0", "0.00", "0.000", "0", "0", 0},
	{1, "1", "1.0", "1.00", "1.000", "1", "1", 1},
	{-1, "-1", "-1.0", "-1.00", "-1.000", "-1", "-1", -1},
	{2.5, "3", "2.5", "2.50", "2.500", "2.5", "2.5", 3},
	{-2.5, "-3", "-2.5", "-2.50", "-2.500", "-2.5", "-2.5", -2},
	{0.5, "1", "0.5", "0.50", "0.500", "0.5", "0.5", 1},
	{-0.4, "-0", "-0.4", "-0.40", "-0.400", "-0.4", "-0.4", 0},
	{1.005, "1", "1.0", "1.00", "1.005", "1.005", "1.005", 1},
	{1.45, "1", "1.4", "1.45", "1.450", "1.45", "1.45", 1},
	{1.55, "2", "1.6", "1.55", "1.550", "1.55", "1.55", 2},
	{123456.789, "123457", "123456.8", "123456.79", "123456.789", "123,456.789", "123456.789", 123457},
	{0.000001234, "0", "0.0", "0.00", "0.000", "0", "0.000001234", 0},
	{1e-7, "0", "0.0", "0.00", "0.000", "0", "1e-7", 0},
	{1234567.891, "1234568", "1234567.9", "1234567.89", "1234567.891", "1,234,567.891", "1234567.891", 1234568},
	{99.995, "100", "100.0", "100.00", "99.995", "99.995", "99.995", 100},
	{0.125, "0", "0.1", "0.13", "0.125", "0.125", "0.125", 0},
	{0.375, "0", "0.4", "0.38", "0.375", "0.375", "0.375", 0},
	{10.235, "10", "10.2", "10.23", "10.235", "10.235", "10.235", 10},
	{8.345, "8", "8.3", "8.35", "8.345", "8.345", "8.345", 8},
	{187263, "187263", "187263.0", "187263.00", "187263.000", "187,263", "187263", 187263},
	{0.0005, "0", "0.0", "0.00", "0.001", "0.001", "0.0005", 0},
	{12.5, "13", "12.5", "12.50", "12.500", "12.5", "12.5", 13},
	{-12.5, "-13", "-12.5", "-12.50", "-12.500", "-12.5", "-12.5", -12},
	{1.0 / 3, "0", "0.3", "0.33", "0.333", "0.333", "0.3333333333333333", 0},
	{2.0 / 3, "1", "0.7", "0.67", "0.667", "0.667", "0.6666666666666666", 1},
	{4100, "4100", "4100.0", "4100.00", "4100.000", "4,100", "4100", 4100},
}

func TestMatchesJavaScript(t *testing.T) {
	for _, c := range cases {
		got := []string{ToFixed(c.X, 0), ToFixed(c.X, 1), ToFixed(c.X, 2), ToFixed(c.X, 3), Locale(c.X), string(AppendFloat(nil, c.X))}
		want := []string{c.F0, c.F1, c.F2, c.F3, c.Loc, c.JSON}
		for i := range got {
			if got[i] != want[i] {
				t.Errorf("%v: [%d] got %q want %q", c.X, i, got[i], want[i])
			}
		}
		if r := Round(c.X); r != c.Round {
			t.Errorf("Round(%v) = %v want %v", c.X, r, c.Round)
		}
	}
}

func TestNumJSON(t *testing.T) {
	type S struct {
		A Num `json:"a"`
		B Num `json:"b"`
		C Num `json:"c"`
	}
	b, _ := json.Marshal(S{A: NaN, B: Num(math.Inf(1)), C: 1.5})
	if string(b) != `{"a":null,"b":null,"c":1.5}` {
		t.Fatal(string(b))
	}
	var s struct {
		A Num `json:"a"`
		B Num `json:"b"`
	}
	type plain = struct {
		A Num `json:"a"`
		B Num `json:"b"`
	}
	if err := DecodeNA([]byte(`{"a": null}`), (*plain)(&s)); err != nil || !math.IsNaN(float64(s.A)) || !math.IsNaN(float64(s.B)) {
		t.Fatalf("missing and null keys must decode as NaN: %+v %v", s, err)
	}
	if Pct(math.NaN(), 1) != "N/A" || Pct(0.9567, 1) != "95.7%" || Ms(412.4) != "412 ms" || Ms(1500) != "1.50 s" || Duration(125) != "2h 5m" {
		t.Fatal("format helpers")
	}
}

func TestLargeValues(t *testing.T) {
	if ToFixed(1e21, 2) != "1e+21" || Locale(1e21) != "1,000,000,000,000,000,000,000" || string(AppendFloat(nil, 1.5e300)) != "1.5e+300" {
		t.Fatal(ToFixed(1e21, 2), Locale(1e21), string(AppendFloat(nil, 1.5e300)))
	}
	if Locale(math.Copysign(0, -1)) != "-0" || ToFixed(math.Copysign(0, -1), 0) != "0" {
		t.Fatal("negative zero")
	}
}
