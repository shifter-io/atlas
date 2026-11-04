package store

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/Zeus-Development/proxy-index-website/api/internal/snap"
	"github.com/jackc/pgx/v5"
)

// A build stored since calculated v8 (proxy-index calculated/parts.py,
// contract K1) keeps a small head in calculated_snapshots.payload: every
// top-level key of the payload except products and runs, plus
// "parts": {"products": n, "runs": m}. Its product groups are the 'product'
// rows of calculated_snapshot_parts, one group per row in payload order, and
// the daily groups' 24-hour series are sparse there: a bucket with no request
// and no availability is left out. The single payload of an older build has
// no "parts" key. SnapshotRow.Payload is the same text either way: the head
// with its groups put back where the single payload has them, each 24-hour
// series with every bucket. The runs are not read: no answer uses them.
//
// snapshotSQL reads the current build's payload, or its head without the
// "parts" key and the head's product part count (-1 when it has none; NULL
// for a single payload).
const snapshotSQL = `SELECT s.build_id, s.content_hash,
 CASE WHEN s.payload ? 'parts' THEN (s.payload - 'parts')::text ELSE s.payload::text END,
 CASE WHEN s.payload ? 'parts' THEN coalesce((s.payload->'parts'->>'products')::int, -1) END
 FROM public.calculated_snapshots s JOIN public.current_calculation c ON c.build_id = s.build_id WHERE c.singleton`

const productPartsSQL = `SELECT part::text FROM public.calculated_snapshot_parts
 WHERE build_id = $1 AND kind = 'product' ORDER BY position`

// productsKey is the key the head leaves out and readParts puts back.
const productsKey = "products"

// readParts sets Payload from the head it holds and the build's product parts,
// read and refilled one group at a time. want is the head's part count; a
// build whose stored parts differ from it is an error.
func (s *SnapshotRow) readParts(ctx context.Context, tx pgx.Tx, want int) error {
	if want < 0 {
		return fmt.Errorf("snapshot %d head: no product part count", s.BuildID)
	}
	rows, err := tx.Query(ctx, productPartsSQL, s.BuildID)
	if err != nil {
		return err
	}
	defer rows.Close()
	n := 0
	payload, err := withProducts(s.Payload, func(out *bytes.Buffer) error {
		for rows.Next() {
			var part []byte
			if err := rows.Scan(&part); err != nil {
				return err
			}
			part, err := dense(part)
			if err != nil {
				return fmt.Errorf("product part %d: %w", n, err)
			}
			if n > 0 {
				out.WriteString(", ")
			}
			out.Write(part)
			n++
		}
		return rows.Err()
	})
	if err != nil {
		return fmt.Errorf("snapshot %d: %w", s.BuildID, err)
	}
	if n != want {
		return fmt.Errorf("snapshot %d: its head counts %d product parts, %d are stored", s.BuildID, want, n)
	}
	s.Payload = payload
	return nil
}

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

// bucket is the 24-hour series' bucket width.
const bucket = 5 * time.Minute

// emptyLatency is a latency block without data.
const emptyLatency = `{"p50": null, "p90": null, "p95": null, "p99": null}`

// emptyPoint is the series point proxy-index aggregate.series() writes for a
// 5-minute bucket without data, as jsonb reads it back (keys shorter first,
// then bytewise; ", " and ": " between them); latency_under_load_ms is there
// when the window has a latency target. These are exactly the points a
// sparse series leaves out (proxy-index calculated/parts.py sparse(); its
// dense() is this file's dense()).
func emptyPoint(at string, timed bool) string {
	p := `{"at": "` + at + `", "uptime": null, "completed": 0, "successes": 0, "latency_ms": ` + emptyLatency +
		`, "unique_ips": null, "success_rate": null`
	if timed {
		p += `, "latency_under_load_ms": ` + emptyLatency
	}
	return p + "}"
}

// pyISO is the stamp Python's datetime.isoformat() writes for t in UTC
// (microseconds only when not zero): a series point's "at".
func pyISO(t time.Time) string {
	t = t.UTC()
	s := t.Format("2006-01-02T15:04:05")
	if us := t.Nanosecond() / 1000; us != 0 {
		s += fmt.Sprintf(".%06d", us)
	}
	return s + "+00:00"
}

// dense is a product group's JSON (as jsonb reads it back) with every
// 24-hour window's series holding every 5-minute bucket from period.start to
// period.end: a series stored sparse gets its empty points back, so the group
// reads exactly as it did when stored whole. A group with nothing missing is
// returned as it is. A stored point off its window's 5-minute grid is an
// error: refilling would drop it.
func dense(product []byte) ([]byte, error) {
	fields, err := objectFields(product)
	if err != nil {
		return nil, err
	}
	f, ok := member(fields, "windows")
	if !ok {
		return product, nil
	}
	windows, err := denseWindows(product[f.Start:f.End])
	if err != nil || windows == nil {
		return product, err
	}
	return splice(product, f, windows), nil
}

// denseWindows is the windows object with its sparse series refilled, nil
// when none is sparse.
func denseWindows(raw []byte) ([]byte, error) {
	fields, err := objectFields(raw)
	if err != nil {
		return nil, err
	}
	var out []byte
	// Last to first, so the earlier windows' offsets stay valid.
	for i := len(fields) - 1; i >= 0; i-- {
		f := fields[i]
		window, err := denseWindow(raw[f.Start:f.End])
		if err != nil {
			return nil, fmt.Errorf("window %s: %w", f.Key, err)
		}
		if window == nil {
			continue
		}
		if out == nil {
			out = raw
		}
		out = splice(out, f, window)
	}
	return out, nil
}

// denseWindow is a 24-hour window with every bucket of its series, nil when
// it is another window or its series is whole.
func denseWindow(raw []byte) ([]byte, error) {
	fields, err := objectFields(raw)
	if err != nil {
		return nil, err
	}
	series, ok := member(fields, "series")
	if !ok {
		return nil, nil
	}
	var period struct {
		Start string   `json:"start"`
		End   string   `json:"end"`
		Hours *float64 `json:"hours"`
	}
	var latency struct {
		Target *string `json:"target"`
	}
	var points []json.RawMessage
	if err := unmarshalAll(valueOf(raw, fields, "period"), &period, valueOf(raw, fields, "latency"), &latency,
		json.RawMessage(raw[series.Start:series.End]), &points); err != nil {
		return nil, err
	}
	if period.Hours == nil || *period.Hours != 24 {
		return nil, nil
	}
	start, okStart := snap.ParseTime(period.Start)
	end, okEnd := snap.ParseTime(period.End)
	if !okStart || !okEnd {
		return nil, fmt.Errorf("period %q to %q", period.Start, period.End)
	}
	buckets := 0
	for at := start; at.Before(end); at = at.Add(bucket) {
		buckets++
	}
	if len(points) == buckets {
		return nil, nil
	}
	whole, err := denseSeries(points, start, end, latency.Target != nil && *latency.Target != "")
	if err != nil {
		return nil, err
	}
	return splice(raw, series, whole), nil
}

// denseSeries is the series text with every bucket, the stored points as they are.
func denseSeries(points []json.RawMessage, start, end time.Time, timed bool) ([]byte, error) {
	kept := make(map[string]json.RawMessage, len(points))
	for _, p := range points {
		var at struct {
			At string `json:"at"`
		}
		if err := json.Unmarshal(p, &at); err != nil {
			return nil, err
		}
		kept[at.At] = p
	}
	if len(kept) != len(points) {
		return nil, errors.New("two series points share a bucket")
	}
	var out bytes.Buffer
	out.WriteByte('[')
	for at := start; at.Before(end); at = at.Add(bucket) {
		if out.Len() > 1 {
			out.WriteString(", ")
		}
		stamp := pyISO(at)
		if p, ok := kept[stamp]; ok {
			out.Write(p)
			delete(kept, stamp)
		} else {
			out.WriteString(emptyPoint(stamp, timed))
		}
	}
	if len(kept) > 0 {
		return nil, fmt.Errorf("%d series points off the %s grid", len(kept), bucket)
	}
	return append(out.Bytes(), ']'), nil
}
