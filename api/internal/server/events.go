package server

import (
	"encoding/json"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/Zeus-Development/atlas/api/internal/build"
	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/store"
)

// parseTapeFilters reads the live tape's parameters into f, and its cursors
// (after, or before: EVENTS-BEFORE-1).
func parseTapeFilters(f *store.EventFilter, get func(string) string, provider func(string) (string, bool)) error {
	if slug := get("network"); slug != "" {
		p, ok := provider(slug)
		if !ok {
			return build.InvalidValue("network", slug)
		}
		f.Provider = p
	}
	if c := get("country"); c != "" {
		if len(c) != 2 || strings.Trim(strings.ToUpper(c), "ABCDEFGHIJKLMNOPQRSTUVWXYZ") != "" {
			return build.InvalidFormat("country", c, build.FormatCountry)
		}
		f.Country = strings.ToUpper(c)
	}
	if f.Target = get("target"); len(f.Target) > maxEventText {
		return build.TooLong("target", maxEventText)
	}
	text := get("q")
	if len(text) > maxQuery {
		return build.TooLong("q", maxQuery)
	}
	if words := strings.Fields(strings.ToLower(text)); len(words) > 0 {
		f.Words = words
	}
	if k := get("proxyKind"); k != "" {
		if !model.IsProxyKind(k) {
			return build.InvalidValue("proxyKind", k, model.ProxyKinds...)
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
				return build.InvalidFormat(c.name, a, build.FormatEventID)
			}
			*c.to = &id
		}
	}
	if f.After != nil && f.Before != nil {
		return build.ConflictingParameters("before", "after")
	}
	return nil
}

// maxEventCodes is how many values kind= and event= take, and maxEventText
// the longest product= and target= (SEC-EVENTS-SAFE-1).
const (
	maxEventCodes = 32
	maxEventText  = 100
)

// parseKinds reads kind= (groups) and event= (event codes, as their raw
// kinds) into f.
func parseKinds(f *store.EventFilter, get func(string) string) error {
	isGroup := func(v string) bool { return slices.Contains(categories, v) }
	groups, err := eventCodesOf("kind", get("kind"), isGroup, func() []string { return categories })
	if err != nil {
		return err
	}
	f.Groups = groups
	events, err := eventCodesOf("event", get("event"), build.IsEvent, build.EventCodeNames)
	for _, c := range events {
		f.Events = append(f.Events, build.EventKind(c))
	}
	return err
}

// eventCodesOf is list's values (codesOf): at most maxEventCodes, each one
// valid takes; another answers invalid_parameter with allowed().
func eventCodesOf(name, list string, valid func(string) bool, allowed func() []string) ([]string, error) {
	values := codesOf(list)
	if len(values) > maxEventCodes {
		return nil, build.TooManyValues(name, maxEventCodes)
	}
	for _, v := range values {
		if !valid(v) {
			return nil, build.InvalidValue(name, v, allowed()...)
		}
	}
	return values, nil
}

// codesOf is a comma-separated list's values, without empty ones.
func codesOf(list string) []string {
	var out []string
	for _, v := range strings.Split(list, ",") {
		if v != "" {
			out = append(out, v)
		}
	}
	return out
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
func parseSince(raw string) (time.Time, error) {
	// URL decoding turns the "+" of "+00:00" into a space.
	s := strings.Replace(strings.TrimSpace(raw), " ", "+", 1)
	for _, layout := range []string{time.RFC3339Nano, "2006-01-02T15:04:05.999999999", "2006-01-02"} {
		if t, err := time.Parse(layout, s); err == nil {
			if t = t.UTC(); t.Equal(t.Truncate(time.Millisecond)) {
				t = t.Add(time.Millisecond - time.Microsecond)
			}
			return t, nil
		}
	}
	return time.Time{}, build.InvalidFormat("since", raw, build.FormatISOTime)
}
