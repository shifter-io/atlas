// Package jsnum holds the numeric contract shared with the website.
//
// The site's TypeScript code uses NaN as the "unavailable" sentinel and
// JSON.stringify turns NaN into null. Num mirrors that exactly: arithmetic
// propagates NaN the way JavaScript does, and the JSON encoding writes null for
// NaN and ±Inf. Decoding null (or a missing key, see DecodeNA) gives NaN back.
//
// Formatting helpers reproduce JavaScript's Number.prototype.toFixed and
// toLocaleString("en-US"), so text the site generates from numbers (verdicts,
// tape rows) is byte-identical when produced here.
package jsnum

import (
	"bytes"
	"encoding/json"
	"math"
	"math/big"
	"reflect"
	"strconv"
	"strings"
)

// Num is a float64 whose NaN/Inf encodes as JSON null.
type Num float64

// NaN is the unavailable value.
var NaN = Num(math.NaN())

// N converts any numeric value.
func N[T ~int | ~int64 | ~int32 | ~float64](v T) Num { return Num(v) }

// Ptr returns NaN for nil.
func Ptr(v *float64) Num {
	if v == nil {
		return NaN
	}
	return Num(*v)
}

// OK reports whether the value is finite.
func (n Num) OK() bool { return !math.IsNaN(float64(n)) && !math.IsInf(float64(n), 0) }

// F returns the float.
func (n Num) F() float64 { return float64(n) }

// MarshalJSON writes null for non-finite values and otherwise follows
// encoding/json's (ES6-compatible) float formatting.
func (n Num) MarshalJSON() ([]byte, error) {
	f := float64(n)
	if math.IsNaN(f) || math.IsInf(f, 0) {
		return []byte("null"), nil
	}
	return AppendFloat(nil, f), nil
}

// AppendFloat formats like JSON.stringify / encoding/json.
func AppendFloat(b []byte, f float64) []byte {
	if f == 0 {
		// JSON.stringify(-0) is "0".
		return append(b, '0')
	}
	abs := math.Abs(f)
	format := byte('f')
	if abs < 1e-6 || abs >= 1e21 {
		format = 'e'
	}
	b = strconv.AppendFloat(b, f, format, -1, 64)
	if format == 'e' {
		// clean up e-09 to e-9
		n := len(b)
		if n >= 4 && b[n-4] == 'e' && b[n-3] == '-' && b[n-2] == '0' {
			b[n-2] = b[n-1]
			b = b[:n-1]
		}
	}
	return b
}

// UnmarshalJSON reads null as NaN.
func (n *Num) UnmarshalJSON(b []byte) error {
	b = bytes.TrimSpace(b)
	if string(b) == "null" {
		*n = NaN
		return nil
	}
	f, err := strconv.ParseFloat(string(b), 64)
	if err != nil {
		return err
	}
	*n = Num(f)
	return nil
}

var numType = reflect.TypeFor[Num]()

// FillNaN sets every Num field reachable through nested structs (not slices,
// maps or pointers) to NaN, so a key that is absent from JSON decodes as
// unavailable rather than zero.
func FillNaN(v any) { fill(reflect.ValueOf(v).Elem()) }

func fill(v reflect.Value) {
	switch v.Kind() {
	case reflect.Struct:
		for i := 0; i < v.NumField(); i++ {
			f := v.Field(i)
			if !f.CanSet() {
				continue
			}
			fill(f)
		}
	case reflect.Array:
		for i := 0; i < v.Len(); i++ {
			fill(v.Index(i))
		}
	case reflect.Float64:
		if v.Type() == numType {
			v.SetFloat(math.NaN())
		}
	}
}

// DecodeNA fills NaN defaults and then decodes. Call it from UnmarshalJSON
// methods on a method-less alias of the receiver type.
func DecodeNA(data []byte, v any) error {
	FillNaN(v)
	return json.Unmarshal(data, v)
}

// --- JavaScript-compatible rounding and formatting -------------------------

// exactDecimal returns the exact decimal expansion of |f| as integer and
// fraction digit strings.
func exactDecimal(f float64) (string, string) {
	// SetFloat64 on a zero-precision Float takes the float's 53 bits; Text('f', n)
	// is exact for n large enough because every float64 has a finite binary expansion.
	s := new(big.Float).SetFloat64(math.Abs(f)).Text('f', 1100)
	parts := strings.SplitN(s, ".", 2)
	frac := ""
	if len(parts) == 2 {
		frac = strings.TrimRight(parts[1], "0")
	}
	return parts[0], frac
}

// roundDecimal rounds the exact decimal value half away from zero (the
// behaviour of toFixed and Intl's default halfExpand) to dp fraction digits.
func roundDecimal(f float64, dp int) (string, string) {
	ip, frac := exactDecimal(f)
	if len(frac) <= dp {
		return ip, frac + strings.Repeat("0", dp-len(frac))
	}
	keep := []byte(ip + frac[:dp])
	if frac[dp] >= '5' {
		// half or more (the exact expansion, so a '5' followed by anything is >= half)
		i := len(keep) - 1
		for i >= 0 {
			if keep[i] == '9' {
				keep[i] = '0'
				i--
				continue
			}
			keep[i]++
			break
		}
		if i < 0 {
			keep = append([]byte{'1'}, keep...)
		}
	}
	n := len(keep) - dp
	return string(keep[:n]), string(keep[n:])
}

// ToFixed reproduces Number.prototype.toFixed for |x| < 1e21.
func ToFixed(x float64, dp int) string {
	if math.IsNaN(x) {
		return "NaN"
	}
	if math.IsInf(x, 1) {
		return "Infinity"
	}
	if math.IsInf(x, -1) {
		return "-Infinity"
	}
	if math.Abs(x) >= 1e21 {
		return string(AppendFloat(nil, x))
	}
	ip, frac := roundDecimal(x, dp)
	sign := ""
	if x < 0 { // toFixed keeps the sign even when the digits round to zero: (-0.4).toFixed(0) is "-0"
		sign = "-"
	}
	if dp == 0 {
		return sign + ip
	}
	return sign + ip + "." + frac
}

// FixedNum is Number(x.toFixed(dp)).
func FixedNum(x float64, dp int) float64 {
	if math.IsNaN(x) || math.IsInf(x, 0) {
		return x
	}
	f, _ := strconv.ParseFloat(ToFixed(x, dp), 64)
	return f
}

// Round is Math.round: floor(x + 0.5), with NaN propagated.
func Round(x float64) float64 {
	if math.IsNaN(x) || math.IsInf(x, 0) {
		return x
	}
	r := math.Floor(x)
	if x-r >= 0.5 {
		r++
	}
	return r
}

// Locale is Number.prototype.toLocaleString("en-US"): grouping and at most
// three fraction digits, half away from zero.
func Locale(x float64) string {
	if math.IsNaN(x) {
		return "NaN"
	}
	if math.IsInf(x, 1) {
		return "∞"
	}
	if math.IsInf(x, -1) {
		return "-∞"
	}
	ip, frac := roundDecimal(x, 3)
	frac = strings.TrimRight(frac, "0")
	var out strings.Builder
	if x < 0 || (x == 0 && math.Signbit(x)) { // Intl writes "-0" for negative values that round to zero
		out.WriteByte('-')
	}
	for i, c := range ip {
		if i > 0 && (len(ip)-i)%3 == 0 {
			out.WriteByte(',')
		}
		out.WriteRune(c)
	}
	if frac != "" {
		out.WriteByte('.')
		out.WriteString(frac)
	}
	return out.String()
}
