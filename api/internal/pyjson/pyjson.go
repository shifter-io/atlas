// Package pyjson writes a JSON document exactly as the framework's Python
// writes it with json.dumps(value, sort_keys=True, separators=(",", ":"),
// allow_nan=False): the bytes a methodology id hashes (calculated/policy.py
// digest), rebuilt from the database's copy of the methodology
// (METHODOLOGY-FILE-1, #1179).
package pyjson

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"slices"
	"strconv"
	"strings"
	"unicode/utf16"
)

// Canonical re-encodes the JSON document doc as Python's json.dumps with
// sorted keys, no spaces and ensure_ascii does: keys in code point order,
// every character outside printable ASCII escaped as \uXXXX (lowercase hex,
// a surrogate pair above U+FFFF), a number with a fraction or an exponent as
// Python's float repr and any other number as written.
func Canonical(doc []byte) ([]byte, error) {
	dec := json.NewDecoder(bytes.NewReader(doc))
	dec.UseNumber()
	var v any
	if err := dec.Decode(&v); err != nil {
		return nil, err
	}
	if dec.More() {
		return nil, errors.New("pyjson: more than one JSON value")
	}
	var buf bytes.Buffer
	if err := write(&buf, v); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}

func write(buf *bytes.Buffer, v any) error {
	switch x := v.(type) {
	case nil:
		buf.WriteString("null")
	case bool:
		buf.WriteString(strconv.FormatBool(x))
	case string:
		writeString(buf, x)
	case json.Number:
		s, err := number(string(x))
		if err != nil {
			return err
		}
		buf.WriteString(s)
	case []any:
		buf.WriteByte('[')
		for i, e := range x {
			if i > 0 {
				buf.WriteByte(',')
			}
			if err := write(buf, e); err != nil {
				return err
			}
		}
		buf.WriteByte(']')
	case map[string]any:
		return writeObject(buf, x)
	default:
		return fmt.Errorf("pyjson: unexpected %T", v)
	}
	return nil
}

func writeObject(buf *bytes.Buffer, m map[string]any) error {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	slices.Sort(keys) // UTF-8 byte order is code point order, as Python sorts str
	buf.WriteByte('{')
	for i, k := range keys {
		if i > 0 {
			buf.WriteByte(',')
		}
		writeString(buf, k)
		buf.WriteByte(':')
		if err := write(buf, m[k]); err != nil {
			return err
		}
	}
	buf.WriteByte('}')
	return nil
}

// short is Python's two-character escapes (json.encoder ESCAPE_DCT).
var short = map[rune]string{'"': `\"`, '\\': `\\`, '\n': `\n`, '\r': `\r`, '\t': `\t`, '\b': `\b`, '\f': `\f`}

// writeString is json.encoder.py_encode_basestring_ascii.
func writeString(buf *bytes.Buffer, s string) {
	buf.WriteByte('"')
	for _, r := range s {
		switch {
		case short[r] != "":
			buf.WriteString(short[r])
		case r >= ' ' && r <= '~':
			buf.WriteRune(r)
		case r > 0xFFFF:
			hi, lo := utf16.EncodeRune(r)
			fmt.Fprintf(buf, `\u%04x\u%04x`, hi, lo)
		default:
			fmt.Fprintf(buf, `\u%04x`, r)
		}
	}
	buf.WriteByte('"')
}

// number is a JSON number as Python writes the value it reads from it: an
// integer as written, anything with a fraction or an exponent as a float.
func number(s string) (string, error) {
	if !strings.ContainsAny(s, ".eE") {
		return s, nil
	}
	f, err := strconv.ParseFloat(s, 64)
	if err != nil {
		return "", fmt.Errorf("pyjson: number %s: %w", s, err)
	}
	return FloatRepr(f), nil
}

// FloatRepr is Python's repr of a finite float (JSON has no other): the shortest digits that read
// back as f, in fixed notation with at least one decimal (30.0, 0.0001) when
// the decimal point falls within 16 digits of them, else as d.ddde±XX
// (1e-05, 1e+16).
func FloatRepr(f float64) string {
	sign := ""
	if math.Signbit(f) {
		sign, f = "-", -f
	}
	sci := strconv.FormatFloat(f, 'e', -1, 64) // d.dddde±XX, the shortest round-trip digits
	mantissa, exp, _ := strings.Cut(sci, "e")
	digits := strings.Replace(mantissa, ".", "", 1)
	e, _ := strconv.Atoi(exp)
	point := e + 1 // digits × 10^(point - len(digits))
	switch {
	case point <= -4 || point > 16:
		out := digits[:1]
		if len(digits) > 1 {
			out += "." + digits[1:]
		}
		esign := "+"
		if e < 0 {
			esign, e = "-", -e
		}
		return fmt.Sprintf("%s%se%s%02d", sign, out, esign, e)
	case point <= 0:
		return sign + "0." + strings.Repeat("0", -point) + digits
	case point >= len(digits):
		return sign + digits + strings.Repeat("0", point-len(digits)) + ".0"
	}
	return sign + digits[:point] + "." + digits[point:]
}
