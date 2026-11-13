package server

import (
	"encoding/json"
	"errors"
	"strconv"
	"strings"

	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/store"
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
