package snap

import (
	"bytes"
	"encoding/json"
	"regexp"
	"time"
)

// JSISO is Date.prototype.toISOString: RFC 3339 in UTC with Z and
// milliseconds, the one form of every time an answer serves (CONSISTENCY-1).
func JSISO(t time.Time) string {
	return t.UTC().Format("2006-01-02T15:04:05.000Z")
}

// ISOText is time text read from the snapshot or the database (any form
// ParseTime reads) in JSISO's form: the same instant to the millisecond.
// Text that is no time stays as it is.
func ISOText(s string) string {
	t, ok := ParseTime(s)
	if !ok {
		return s
	}
	return JSISO(t)
}

// isoString is a JSON string that starts as an ISO time.
var isoString = regexp.MustCompile(`"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}[^"\\]*"`)

// ISOInJSON is JSON b with every string that is a time (ISOText) in JSISO's
// form, the rest byte for byte: an event's data, as the framework wrote it.
func ISOInJSON(b []byte) []byte {
	if !isoString.Match(b) {
		return b
	}
	return isoString.ReplaceAllFunc(b, func(m []byte) []byte {
		return append(append([]byte{'"'}, ISOText(string(bytes.Trim(m, `"`)))...), '"')
	})
}

// Time is a time served in JSISO's form; it decodes any RFC 3339 time.
type Time time.Time

func (t Time) MarshalJSON() ([]byte, error) { return json.Marshal(JSISO(time.Time(t))) }

func (t *Time) UnmarshalJSON(b []byte) error { return (*time.Time)(t).UnmarshalJSON(b) }

// At is t as a Time.
func At(t time.Time) *Time {
	v := Time(t)
	return &v
}
