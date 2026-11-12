package build

import (
	"math"
	"time"

	"github.com/Zeus-Development/proxy-index/api/internal/jsnum"
	"github.com/Zeus-Development/proxy-index/api/internal/model"
)

// EventValues is model.EventValues.
type EventValues = model.EventValues

// eventValues is the values of an event of kind from its raw data, made here
// alone so a tape row and its /v1/events row always agree.
func eventValues(kind string, d map[string]any) EventValues {
	if fn, ok := valueMakers[kind]; ok {
		return fn(d)
	}
	return EventValues{}
}

// tapeContractNull are the tape row fields whose nulls are part of the
// contract (TAPE-VALUES-1), never listed in its na.
var tapeContractNull = []string{"country", "region", "target", "severity", "values"}

// valueMakers holds one function per event kind with numbers.
var valueMakers = map[string]func(d map[string]any) EventValues{
	"pulse_finished": pulseFinishedValues,
	"latency_spike": func(d map[string]any) EventValues {
		v := pulseValues(d)
		if !v["p50"].(jsnum.Num).OK() {
			v["p50"] = num(d, "value")
		}
		v["baseline"] = num(d, "baseline")
		return v
	},
	"down":         rateValues,
	"success_drop": rateValues,
	"recovered":    rateValues,
	"block_spike": func(d map[string]any) EventValues {
		v := pulseValues(d)
		v["blockShare"], v["baselineBlockShare"] = num(d, "value"), num(d, "baseline")
		return v
	},
	"estimate_hour_released": func(d map[string]any) EventValues {
		return EventValues{"hour": hourISO(d), "uniqueIps": num(d, "unique_ips"), "countries": num(d, "countries")}
	},
	"daily_test_started": func(d map[string]any) EventValues {
		return EventValues{"countries": lenOf(d["countries"])}
	},
	"daily_test_finished": func(d map[string]any) EventValues {
		return EventValues{"countries": lenOf(d["countries"]), "requests": num(d, "requests"),
			"uniqueIps": num(d, "unique_ips"), "durationS": num(d, "duration_s")}
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
		return EventValues{"requests": num(d, "requests"), "uniqueIps": num(d, "unique_ips"), "durationS": num(d, "duration_s")}
	},
}

// pulseValues is what every pulse-judged kind carries: its pulse's counts,
// p50 (ms) and a null baseline.
func pulseValues(d map[string]any) EventValues {
	return EventValues{"requests": num(d, "requests"), "successes": num(d, "successes"), "p50": num(d, "p50"), "baseline": jsnum.NaN}
}

// pulseFinishedValues adds the previous pulse's p50 and success share and
// the pulse's challenges and blocks (the raw previous has no success count).
func pulseFinishedValues(d map[string]any) EventValues {
	v := pulseValues(d)
	v["challenges"], v["blocks"] = num(d, "challenges"), num(d, "blocks")
	v["previousP50"], v["previousSuccessRate"] = jsnum.NaN, jsnum.NaN
	if prev, ok := d["previous"].(map[string]any); ok {
		v["previousP50"], v["previousSuccessRate"] = num(prev, "p50"), num(prev, "success_rate")
	}
	return v
}

// rateValues is down, success_drop and recovered: the raw baseline is a
// success share (0 to 1).
func rateValues(d map[string]any) EventValues {
	v := pulseValues(d)
	v["baselineRate"] = num(d, "baseline")
	return v
}

func durationValues(d map[string]any) EventValues {
	return EventValues{"durationS": num(d, "duration_s")}
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

// hourISO is an hour's release as the start of the hour (its day and hour,
// UTC), or null.
func hourISO(d map[string]any) *string {
	day, _ := strOf(d["day"])
	h, ok := numOf(d["hour"])
	t, err := time.Parse(time.DateOnly, day)
	if !ok || err != nil || h != math.Trunc(h) || h < 0 || h > 23 {
		return nil
	}
	s := JSISO(t.Add(time.Duration(h) * time.Hour))
	return &s
}
