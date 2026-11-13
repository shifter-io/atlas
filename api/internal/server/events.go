package server

import (
	"encoding/json"
	"errors"
	"strconv"
	"strings"
	"time"

	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/Zeus-Development/proxy-index/api/internal/store"
)

// parseTapeFilters reads the live tape's parameters into f, and its cursors
// (after, or before: EVENTS-BEFORE-1).
func parseTapeFilters(f *store.EventFilter, get func(string) string, provider func(string) (string, bool)) error {
	if slug := get("network"); slug != "" {
		p, ok := provider(slug)
		if !ok {
			return errors.New("unknown network " + slug)
		}
		f.Provider = p
	}
	if c := get("country"); c != "" {
		if len(c) != 2 || strings.Trim(strings.ToUpper(c), "ABCDEFGHIJKLMNOPQRSTUVWXYZ") != "" {
			return errors.New("country must be a two-letter code")
		}
		f.Country = strings.ToUpper(c)
	}
	f.Target = get("target")
	text := get("q")
	if len(text) > 200 {
		return errors.New("q too long")
	}
	if words := strings.Fields(strings.ToLower(text)); len(words) > 0 {
		f.Words = words
	}
	if k := get("proxyKind"); k != "" {
		if !model.IsProxyKind(k) {
			return errors.New("proxyKind must be " + strings.Join(model.ProxyKinds, ", "))
		}
		f.ProxyKind = k
	}
	for _, c := range []struct {
		name string
		to   **int64
	}{{"after", &f.After}, {"before", &f.Before}} {
		if a := get(c.name); a != "" {
			id, err := strconv.ParseInt(a, 10, 64)
			if err != nil || id < 0 {
				return errors.New(c.name + " must be an event id")
			}
			*c.to = &id
		}
	}
	if f.After != nil && f.Before != nil {
		return errors.New("before and after cannot be combined")
	}
	return nil
}

// eventsKey identifies a filter in the events LRU.
func eventsKey(f store.EventFilter) string {
	key, _ := json.Marshal(f) //nolint:errcheck // a filter always encodes
	return string(key)
}

// parseSince reads since in UTC. A since of whole milliseconds stands for
// the end of its millisecond: an event's at is served to the millisecond
// (CONSISTENCY-1), so a client polling since the newest at it was served
// gets none of that millisecond's events again. A since with finer digits
// (the microseconds served before) stays as it is.
func parseSince(s string) (time.Time, error) {
	// URL decoding turns the "+" of "+00:00" into a space.
	s = strings.Replace(strings.TrimSpace(s), " ", "+", 1)
	for _, layout := range []string{time.RFC3339Nano, "2006-01-02T15:04:05.999999999", "2006-01-02"} {
		if t, err := time.Parse(layout, s); err == nil {
			if t = t.UTC(); t.Equal(t.Truncate(time.Millisecond)) {
				t = t.Add(time.Millisecond - time.Microsecond)
			}
			return t, nil
		}
	}
	return time.Time{}, errors.New("since must be an ISO time")
}
