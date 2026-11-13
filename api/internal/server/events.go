package server

import (
	"encoding/json"
	"slices"
	"strconv"
	"strings"

	"github.com/Zeus-Development/proxy-index/api/internal/build"
	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/Zeus-Development/proxy-index/api/internal/store"
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
	f.Target = get("target")
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

// parseKinds reads kind= (event codes and groups: a row passes with either)
// and event= (event codes, as their raw kinds) into f.
func parseKinds(f *store.EventFilter, get func(string) string) {
	for _, k := range codesOf(get("kind")) {
		if slices.Contains(categories, k) {
			f.Groups = append(f.Groups, k)
		} else {
			f.Kinds = append(f.Kinds, k)
		}
	}
	for _, c := range codesOf(get("event")) {
		f.Events = append(f.Events, build.EventKind(c))
	}
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
