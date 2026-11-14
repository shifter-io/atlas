package build

import (
	"bytes"
	"encoding/json"
	"regexp"
	"slices"
	"strings"
)

// The published part of an event (SEC-EVENTS-SAFE-1): its values are made
// from the data keys registered for its kind (eventData) only, and keep only
// its code's parameters and the keys every row carries. No answer serves the
// data itself (#993).

// dataRule is how an eventData key is kept: always, or only as a code.
type dataRule struct{ codeOnly bool }

// dataRules is eventData by kind and key.
var dataRules = func() map[string]map[string]dataRule {
	out := map[string]map[string]dataRule{}
	for kind, list := range eventData {
		out[kind] = map[string]dataRule{}
		for _, k := range list {
			name, code := strings.CutSuffix(k, codeOnly)
			out[kind][name] = dataRule{code}
		}
	}
	return out
}()

// codeValue is a JSON string that is a code: lower case, digits and _.
var codeValue = regexp.MustCompile(`^"[a-z][a-z0-9_]{0,39}"$`)

// publicData is raw (an event's data object) with only the keys registered
// for kind, in their stored order; {} when raw is no object.
func publicData(kind string, raw json.RawMessage) json.RawMessage {
	rules := dataRules[kind]
	dec := json.NewDecoder(bytes.NewReader(raw))
	if t, err := dec.Token(); err != nil || t != json.Delim('{') {
		return json.RawMessage("{}")
	}
	out := []byte{'{'}
	for dec.More() {
		t, err := dec.Token()
		var v json.RawMessage
		if err != nil || dec.Decode(&v) != nil {
			return json.RawMessage("{}")
		}
		k, _ := t.(string)
		rule, ok := rules[k]
		if !ok || (rule.codeOnly && !codeValue.Match(v)) {
			continue
		}
		if len(out) > 1 {
			out = append(out, ',')
		}
		key, _ := json.Marshal(k) //nolint:errcheck // a string always encodes
		out = append(append(append(out, key...), ':'), v...)
	}
	return append(out, '}')
}

// kindParams is each raw event kind's parameter names (codes_events.go).
var kindParams = func() map[string][]string {
	out := map[string][]string{}
	for _, c := range eventCodes() {
		names := []string{}
		for _, p := range c.Params {
			names = append(names, p.Name)
		}
		out[EventKind(c.Code)] = names
	}
	return out
}()

// rowValueKeys are the values keys every row carries (eventValues).
var rowValueKeys = []string{"requests", "successes", "p50", "baseline", "value", "uniqueIps", "sharedIps", "hour"}

// registeredValues drops from v every key that is neither one of kind's
// parameters nor a key every row carries.
func registeredValues(kind string, v EventValues) EventValues {
	params := kindParams[kind]
	for k := range v {
		if !slices.Contains(params, k) && !slices.Contains(rowValueKeys, k) {
			delete(v, k)
		}
	}
	return v
}

// IsEvent reports whether s is an event code, or the raw kind one stands for.
func IsEvent(s string) bool {
	_, ok := kindParams[EventKind(s)]
	return ok
}

// EventCodeNames is every event code, in the registry's order.
func EventCodeNames() []string {
	out := []string{}
	for _, c := range eventCodes() {
		out = append(out, c.Code)
	}
	return out
}

// EventDataKeys is each event code's data keys values read, for api/CODES.md
// (a key kept only as a code ends in codeOnly).
func EventDataKeys() map[string][]string {
	out := map[string][]string{}
	for kind, list := range eventData {
		out[tapeEvent(kind)] = list
	}
	return out
}
