package build

import (
	"maps"
	"math"
	"time"

	"github.com/Zeus-Development/atlas/api/internal/jsnum"
	"github.com/Zeus-Development/atlas/api/internal/model"
)

// EventValues is model.EventValues.
type EventValues = model.EventValues

// eventValues is the values of an event of kind from its raw data, made here
// alone so a tape row and its /v1/events row always agree: the keys every
// row carries (null where the kind has none; hour wherever the data has one),
// its kind's own keys, and value, its numeric headline (headlines); no key
// its code does not register (registeredValues). A pulse-judged success kind
// (successKinds) has neither value nor baseline: its counts stay.
func eventValues(kind string, d map[string]any) EventValues {
	v := EventValues{}
	for _, k := range rowValueKeys {
		v[k] = jsnum.NaN
	}
	v["hour"] = hourISO(d)
	if fn, ok := valueMakers[kind]; ok {
		maps.Copy(v, fn(d))
	}
	if fn, ok := headlines[kind]; ok {
		v["value"] = fn(d)
	}
	if successKinds[kind] {
		delete(v, "value")
		delete(v, "baseline")
	}
	return registeredValues(kind, v)
}

// successKinds are the kinds whose value and baseline were success shares:
// no success rate is served (SUCCESS-CURVE-1, #1072, after #900), so their
// values keep only the counts (requests, successes) and their own keys.
var successKinds = map[string]bool{"pulse_finished": true, "down": true, "success_drop": true, "recovered": true}

// headlines is each kind's value: the number its tape text value shows.
var headlines = map[string]func(d map[string]any) jsnum.Num{
	"latency_spike":          func(d map[string]any) jsnum.Num { return either(d, "value", "p50") },
	"latency_rise":           func(d map[string]any) jsnum.Num { return num(d, "p50") },
	"block_spike":            func(d map[string]any) jsnum.Num { return num(d, "value") },
	"ip_overlap":             func(d map[string]any) jsnum.Num { return num(d, "value") },
	"estimate_hour_released": func(d map[string]any) jsnum.Num { return num(d, "unique_ips") },
	"daily_test_finished":    func(d map[string]any) jsnum.Num { return num(d, "unique_ips") },
	"census_finished":        func(d map[string]any) jsnum.Num { return num(d, "unique_ips") },
	"daily_test_started":     func(d map[string]any) jsnum.Num { return lenOf(d["countries"]) },
	"new_hosting_asn":        func(d map[string]any) jsnum.Num { return num(d, "ips") },
}

// either is d[a], else d[b], else null.
func either(d map[string]any, a, b string) jsnum.Num {
	if v := num(d, a); v.OK() {
		return v
	}
	return num(d, b)
}

// tapeContractNull are the tape row fields whose nulls are part of the
// contract (TAPE-VALUES-1), never listed in its na.
var tapeContractNull = []string{"country", "region", "target", "severity", "values"}

// valueMakers holds one function per event kind with numbers: its keys.
var valueMakers = map[string]func(d map[string]any) EventValues{
	"pulse_finished": pulseFinishedValues,
	"latency_spike": func(d map[string]any) EventValues {
		v := pulseValues(d)
		v["p50"], v["baseline"] = either(d, "p50", "value"), num(d, "baseline")
		return v
	},
	"down":         pulseValues,
	"success_drop": pulseValues,
	"recovered": func(d map[string]any) EventValues {
		v := pulseValues(d)
		v["durationSeconds"] = num(d, "duration_s")
		return v
	},
	"block_spike": func(d map[string]any) EventValues {
		v := pulseValues(d)
		v["blockShare"], v["baselineBlockShare"] = num(d, "value"), num(d, "baseline")
		return v
	},
	"estimate_hour_released": func(d map[string]any) EventValues {
		return EventValues{"uniqueIps": num(d, "unique_ips"), "countries": num(d, "countries")}
	},
	"daily_test_started": func(d map[string]any) EventValues {
		return EventValues{"countries": lenOf(d["countries"])}
	},
	"daily_test_finished": func(d map[string]any) EventValues {
		return EventValues{"countries": lenOf(d["countries"]), "requests": num(d, "requests"),
			"uniqueIps": num(d, "unique_ips"), "durationSeconds": num(d, "duration_s")}
	},
	"ip_overlap": func(d map[string]any) EventValues {
		return EventValues{"share": num(d, "value"), "sharedIps": num(d, "shared_ips"), "uniqueIps": num(d, "unique_ips"),
			"peers": lenOf(d["peers"]), "runs": num(d, "runs")}
	},
	"new_hosting_asn": func(d map[string]any) EventValues {
		return EventValues{"asn": num(d, "asn")}
	},
	"pulse_down": func(d map[string]any) EventValues {
		return EventValues{"requests": num(d, "requests"), "errors": num(d, "errors"),
			"windowMinutes": num(d, "window_minutes"), "regions": num(d, "regions")}
	},
	"pulse_recovered": durationValues,
	"latency_rise": func(d map[string]any) EventValues {
		return EventValues{"p50": num(d, "p50"), "baseline": num(d, "baseline")}
	},
	"census_finished": func(d map[string]any) EventValues {
		return EventValues{"requests": num(d, "requests"), "uniqueIps": num(d, "unique_ips"), "durationSeconds": num(d, "duration_s")}
	},
}

// pulseValues is what every pulse-judged kind carries: its pulse's counts,
// p50 (ms) and a null baseline.
func pulseValues(d map[string]any) EventValues {
	return EventValues{"requests": num(d, "requests"), "successes": num(d, "successes"), "p50": num(d, "p50"), "baseline": jsnum.NaN}
}

// pulseFinishedValues adds the previous pulse's p50 and counts (TAPE-VALUES-2:
// counts, never its rate; null on events stored before the framework wrote
// them) and the pulse's challenges and blocks.
func pulseFinishedValues(d map[string]any) EventValues {
	v := pulseValues(d)
	v["challenges"], v["blocks"] = num(d, "challenges"), num(d, "blocks")
	prev, _ := d["previous"].(map[string]any)
	v["previousP50"], v["previousSuccesses"], v["previousRequests"] = num(prev, "p50"), num(prev, "successes"), num(prev, "requests")
	return v
}

func durationValues(d map[string]any) EventValues {
	return EventValues{"durationSeconds": num(d, "duration_s")}
}

// num is d[key] when it is a finite number, else null.
func num(d map[string]any, key string) jsnum.Num {
	if f, ok := numOf(d[key]); ok {
		return jsnum.Num(f)
	}
	return jsnum.NaN
}

// lenOf is the length of a list or an object, else null.
func lenOf(v any) jsnum.Num {
	switch x := v.(type) {
	case []any:
		return jsnum.N(len(x))
	case map[string]any:
		return jsnum.N(len(x))
	}
	return jsnum.NaN
}

// hourISO is the start of an event's hour: its ISO time (an audit's), or
// its day and hour of the day (a release's), UTC; else null.
func hourISO(d map[string]any) *string {
	if at, ok := strOf(d["hour"]); ok {
		t, err := time.Parse(time.RFC3339Nano, at)
		if err != nil {
			return nil
		}
		s := JSISO(t)
		return &s
	}
	day, _ := strOf(d["day"])
	h, ok := numOf(d["hour"])
	t, err := time.Parse(time.DateOnly, day)
	if !ok || err != nil || h != math.Trunc(h) || h < 0 || h > 23 {
		return nil
	}
	s := JSISO(t.Add(time.Duration(h) * time.Hour))
	return &s
}
