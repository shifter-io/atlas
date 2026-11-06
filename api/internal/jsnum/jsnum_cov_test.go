package jsnum

import (
	"encoding/json"
	"math"
	"testing"
)

func TestNumHelpers(t *testing.T) {
	if N(3) != 3 || N(int64(-4)) != -4 || N(int32(5)) != 5 || N(2.5) != 2.5 {
		t.Fatal("N converts every numeric type")
	}
	if Ptr(nil).OK() {
		t.Fatal("Ptr(nil) is not NaN")
	}
	f := 1.25
	if Ptr(&f) != 1.25 {
		t.Fatal("Ptr(&1.25)")
	}
	for _, n := range []Num{NaN, Num(math.Inf(1)), Num(math.Inf(-1))} {
		if n.OK() {
			t.Errorf("%v is OK", n)
		}
	}
	if !Num(0).OK() || !Num(-1e300).OK() {
		t.Fatal("finite values are OK")
	}
	if Num(7.5).F() != 7.5 || !math.IsNaN(NaN.F()) {
		t.Fatal("F returns the float")
	}
}

func TestNumUnmarshal(t *testing.T) {
	var n Num
	for _, c := range []struct {
		in   string
		want float64 // NaN: null
	}{
		{"1.5", 1.5},
		{" null ", math.NaN()},
		{"-2e3", -2000},
	} {
		if err := n.UnmarshalJSON([]byte(c.in)); err != nil {
			t.Fatalf("%q: %v", c.in, err)
		}
		if math.IsNaN(c.want) != math.IsNaN(n.F()) || !math.IsNaN(c.want) && n.F() != c.want {
			t.Errorf("%q decoded %v, want %v", c.in, n, c.want)
		}
	}
	n = 9
	if err := n.UnmarshalJSON([]byte(`"9"`)); err == nil || n != 9 {
		t.Fatalf("a string decoded: %v %v", n, err)
	}
	if err := json.Unmarshal([]byte(`{"a":true}`), &struct {
		A Num `json:"a"`
	}{}); err == nil {
		t.Fatal("a bool decoded")
	}
}

// FillNaN reaches nested structs and arrays, and leaves pointers, slices,
// maps, plain floats and unexported fields alone.
func TestFillNaNReach(t *testing.T) {
	type inner struct{ X Num }
	var v struct {
		A       Num
		Plain   float64
		In      inner
		Arr     [2]Num
		InArr   [1]inner
		Ptr     *Num
		Slice   []Num
		Map     map[string]Num
		private Num
	}
	FillNaN(&v)
	if v.A.OK() || v.In.X.OK() || v.Arr[0].OK() || v.Arr[1].OK() || v.InArr[0].X.OK() {
		t.Fatalf("a reachable Num is not NaN: %+v", v)
	}
	if v.Plain != 0 || v.Ptr != nil || v.Slice != nil || v.Map != nil || v.private != 0 {
		t.Fatalf("FillNaN touched something it must not: %+v", v)
	}
}

func TestToFixedNonFinite(t *testing.T) {
	if ToFixed(math.NaN(), 2) != "NaN" || ToFixed(math.Inf(1), 0) != "Infinity" || ToFixed(math.Inf(-1), 1) != "-Infinity" {
		t.Fatal("non-finite toFixed")
	}
	if ToFixed(-1e21, 0) != "-1e+21" || ToFixed(9.995, 2) != "9.99" || ToFixed(0.995, 2) != "0.99" || ToFixed(9.5, 0) != "10" {
		t.Fatal(ToFixed(-1e21, 0), ToFixed(9.995, 2), ToFixed(0.995, 2), ToFixed(9.5, 0))
	}
	// Rounding carries through every digit.
	if ToFixed(99.96, 1) != "100.0" || ToFixed(-999.5, 0) != "-1000" {
		t.Fatal(ToFixed(99.96, 1), ToFixed(-999.5, 0))
	}
}

func TestFixedNumAndRound(t *testing.T) {
	for _, c := range []struct {
		x    float64
		dp   int
		want float64
	}{
		{1.005, 2, 1},
		{1.255, 1, 1.3},
		{-2.5, 0, -3},
		{123.456, 0, 123},
	} {
		if got := FixedNum(c.x, c.dp); got != c.want {
			t.Errorf("FixedNum(%v, %d) = %v, want %v", c.x, c.dp, got, c.want)
		}
	}
	if !math.IsNaN(FixedNum(math.NaN(), 2)) || !math.IsInf(FixedNum(math.Inf(-1), 2), -1) {
		t.Fatal("FixedNum passes non-finite values through")
	}
	if !math.IsNaN(Round(math.NaN())) || !math.IsInf(Round(math.Inf(1)), 1) {
		t.Fatal("Round passes non-finite values through")
	}
}

func TestLocaleNonFinite(t *testing.T) {
	if Locale(math.NaN()) != "NaN" || Locale(math.Inf(1)) != "∞" || Locale(math.Inf(-1)) != "-∞" {
		t.Fatal("non-finite toLocaleString")
	}
	if Locale(-1234.5678) != "-1,234.568" || Locale(999.9996) != "1,000" || Locale(100) != "100" {
		t.Fatal(Locale(-1234.5678), Locale(999.9996), Locale(100))
	}
}

// The format.ts helpers: N/A for a missing value, otherwise the site's text.
func TestFormatHelpers(t *testing.T) {
	nan, inf := math.NaN(), math.Inf(1)
	for _, c := range []struct{ got, want string }{
		{Count(nan), NA},
		{Count(inf), NA},
		{Count(1234567), "1,234,567"},
		{Count(0.5), "0.5"},
		{Ms(nan), NA},
		{Ms(999.4), "999 ms"},
		{Ms(999.5), "1000 ms"},
		{Ms(1000), "1.00 s"},
		{Ms(12346), "12.35 s"},
		{Duration(nan), NA},
		{Duration(0), "0 min"},
		{Duration(59.4), "59 min"},
		{Duration(60), "1h"},
		{Duration(120.2), "2h"},
		{Duration(150), "2h 30m"},
		{Money(nan, -1), NA},
		{Money(0.005, -1), "$0.0050"},
		{Money(0.5, -1), "$0.500"},
		{Money(3.456, -1), "$3.46"},
		{Money(3.456, 0), "$3"},
		{Money(3, 2), "$3.00"},
		{PriceRange(nan, nan), NA},
		{PriceRange(0.75, nan), "$0.75/GB"},
		{PriceRange(0.75, 0.75), "$0.75/GB"},
		{PriceRange(nan, 1), "$1.00/GB"},
		{PriceRange(0.75, 1), "$0.75–$1.00/GB"},
		{Pct(inf, 0), NA},
		{Pct(0.5, 0), "50%"},
	} {
		if c.got != c.want {
			t.Errorf("got %q, want %q", c.got, c.want)
		}
	}
	if !Missing(nan) || !Missing(-inf) || Missing(0) {
		t.Fatal("Missing")
	}
}
