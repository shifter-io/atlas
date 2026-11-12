package build

import (
	"encoding/json"
	"fmt"
	"math"
	"strings"

	"github.com/Zeus-Development/proxy-index/api/internal/jsnum"
	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/Zeus-Development/proxy-index/api/internal/snap"
	"github.com/Zeus-Development/proxy-index/api/internal/store"
)

// TapeProduct is tape.ts TapeProduct.
type TapeProduct struct {
	Provider, Name, Kind string
}

func numOf(v any) (float64, bool) {
	f, ok := v.(float64)
	if !ok || math.IsNaN(f) || math.IsInf(f, 0) {
		return 0, false
	}
	return f, true
}

func strOf(v any) (string, bool) {
	s, ok := v.(string)
	return s, ok
}

// place is tape.ts place: country, else region, else data.region.
func place(e *store.EventJSON, data map[string]any) string {
	if e.Country != nil && *e.Country != "" {
		return *e.Country
	}
	if e.Region != nil && *e.Region != "" {
		return *e.Region
	}
	if s, ok := strOf(data["region"]); ok {
		return s
	}
	return ""
}

type tapeText struct{ title, detail, value string }

// runTime and joinParts are tape.ts runTime and joined (plural is in measured.go).
func runTime(seconds float64) string {
	if seconds < 60 {
		return "<1 min"
	}
	return jsnum.Duration(seconds / 60)
}

func joinParts(parts ...string) string {
	out := []string{}
	for _, p := range parts {
		if p != "" {
			out = append(out, p)
		}
	}
	return strings.Join(out, " · ")
}

// describeDaily is tape.ts describeDaily: the daily test and hourly update
// rows. An hour's release reads as an hourly update: no tape text names the
// estimate (owner decision, API-ESTIMATES-1).
func describeDaily(e *store.EventJSON, d map[string]any, inWhere string) (tapeText, bool) {
	countries := ""
	if list, ok := d["countries"].([]any); ok {
		countries = plural(float64(len(list)), "country", "countries")
	}
	ipsText := jsnum.NA
	if ips, ok := numOf(d["unique_ips"]); ok {
		ipsText = jsnum.Count(ips) + " IPs"
	}
	switch e.Kind {
	case "daily_test_started":
		return tapeText{"Daily test started" + inWhere, countries, "Started"}, true
	case "daily_test_finished":
		requests, seconds := "", ""
		if n, ok := numOf(d["requests"]); ok {
			requests = jsnum.Count(n) + " requests"
		}
		if n, ok := numOf(d["duration_s"]); ok {
			seconds = runTime(n)
		}
		detail := joinParts(countries, requests, seconds)
		if s, _ := strOf(d["status"]); s == "failed" {
			return tapeText{"Daily test failed" + inWhere, detail, "Failed"}, true
		}
		return tapeText{"Daily test done" + inWhere, detail, ipsText}, true
	case "estimate_hour_released":
		title := "Hourly update released"
		if h, ok := numOf(d["hour"]); ok && h == math.Trunc(h) && h >= 0 && h <= 23 {
			title = fmt.Sprintf("Hourly update for %02d:00 UTC", int(h))
		}
		day, _ := strOf(d["day"])
		return tapeText{title, day, ipsText}, true
	}
	return tapeText{}, false
}

// describeOutage is tape.ts describeOutage: pulse outages and latency rises.
func describeOutage(e *store.EventJSON, d map[string]any, inWhere string) (tapeText, bool) {
	switch e.Kind {
	case "pulse_down":
		failed, window, regions := "", "", ""
		errors, okErrors := numOf(d["errors"])
		if requests, ok := numOf(d["requests"]); ok && okErrors {
			failed = jsnum.Count(errors) + " of " + jsnum.Count(requests) + " failed"
		}
		if n, ok := numOf(d["window_minutes"]); ok {
			window = jsnum.Duration(n) + " window"
		}
		if n, ok := numOf(d["regions"]); ok {
			regions = plural(n, "region", "regions")
		}
		return tapeText{"Pulse down" + inWhere, joinParts(failed, window, regions), "Down"}, true
	case "pulse_recovered":
		detail := ""
		if n, ok := numOf(d["duration_s"]); ok {
			detail = "down " + runTime(n)
		}
		return tapeText{"Pulse recovered" + inWhere, detail, "Recovered"}, true
	case "latency_rise":
		detail := ""
		if b, ok := numOf(d["baseline"]); ok {
			detail = "baseline " + jsnum.Ms(b)
		}
		p50, ok := numOf(d["p50"])
		if !ok {
			return tapeText{"Latency rose" + inWhere, detail, jsnum.NA}, true
		}
		return tapeText{"Latency rose to " + jsnum.Ms(p50) + inWhere, detail, jsnum.Ms(p50)}, true
	}
	return tapeText{}, false
}

// tapeFacts holds what describe reads for more than one event kind.
type tapeFacts struct {
	e                                             *store.EventJSON
	d                                             map[string]any
	where, inWhere, baselineText                  string
	requests, seconds, rate, baseline             float64
	hasRequests, hasSeconds, hasRate, hasBaseline bool
}

func newTapeFacts(e *store.EventJSON, d map[string]any) *tapeFacts {
	f := &tapeFacts{e: e, d: d, where: place(e, d)}
	if f.where != "" {
		f.inWhere = " in " + f.where
	}
	f.requests, f.hasRequests = numOf(d["requests"])
	f.seconds, f.hasSeconds = numOf(d["duration_s"])
	f.rate, f.hasRate = numOf(d["success_rate"])
	if !f.hasRate {
		f.rate, f.hasRate = numOf(d["value"])
	}
	f.baseline, f.hasBaseline = numOf(d["baseline"])
	if f.hasBaseline {
		f.baselineText = "baseline " + jsnum.Pct(f.baseline, 0)
	}
	return f
}

// describers holds one function per event kind; describeDaily and
// describeOutage cover the kinds missing here.
var describers = map[string]func(f *tapeFacts) tapeText{
	"census_started":  describeCensusStarted,
	"census_finished": describeCensusFinished,
	"census_retry":    describeCensusRetry,
	"census_skipped":  describeCensusSkipped,
	"pulse_finished":  describePulseFinished,
	"success_drop":    describeSuccessDrop,
	"latency_spike":   describeLatencySpike,
	"block_spike":     describeBlockSpike,
	"down":            describeDown,
	"recovered":       describeRecovered,
	"new_hosting_asn": describeNewHostingASN,
	"ip_overlap":      describeIPOverlap,
	"node_offline": func(f *tapeFacts) tapeText {
		return tapeText{"Node offline" + f.inWhere, "", "Offline"}
	},
	"no_node": func(f *tapeFacts) tapeText {
		return tapeText{"No node available" + f.inWhere, "", "No node"}
	},
}

func describe(e *store.EventJSON, d map[string]any) tapeText {
	f := newTapeFacts(e, d)
	if t, ok := describeDaily(e, d, f.inWhere); ok {
		return t
	}
	if t, ok := describeOutage(e, d, f.inWhere); ok {
		return t
	}
	if fn, ok := describers[e.Kind]; ok {
		return fn(f)
	}
	return tapeText{strings.ReplaceAll(e.Kind, "_", " "), "", jsnum.NA}
}

func describeCensusStarted(f *tapeFacts) tapeText {
	e := f.e
	extra := ""
	if e.Country != nil && *e.Country != "" && e.Region != nil && *e.Region != "" {
		extra = " (" + *e.Region + ")"
	}
	target := ""
	if e.Target != nil {
		target = *e.Target
	}
	return tapeText{"Census started" + f.inWhere + extra, target, "Started"}
}

func describeCensusFinished(f *tapeFacts) tapeText {
	parts := []string{}
	if f.hasRequests {
		parts = append(parts, jsnum.Count(f.requests)+" requests")
	}
	if f.hasSeconds {
		parts = append(parts, runTime(f.seconds))
	}
	if s, _ := strOf(f.d["stop_reason"]); s == "stalled" {
		return tapeText{"Census stopped" + f.inWhere + ": network down", joinParts(parts...), "Down"}
	}
	value := jsnum.NA
	if ips, ok := numOf(f.d["unique_ips"]); ok {
		value = jsnum.Count(ips) + " IPs"
	}
	return tapeText{"Census done" + f.inWhere, joinParts(parts...), value}
}

// describeCensusRetry: a census the provider spoiled (no success, stalled),
// run again once a pulse got through.
func describeCensusRetry(f *tapeFacts) tapeText {
	detail := ""
	if reason, _ := strOf(f.d["reason"]); reason != "" {
		detail = "after " + strings.ReplaceAll(reason, "_", " ")
	}
	return tapeText{"Census rerun" + f.inWhere, detail, "Rerun"}
}

func describeCensusSkipped(f *tapeFacts) tapeText {
	reason, _ := strOf(f.d["reason"])
	return tapeText{"Census skipped" + f.inWhere, reason, "Skipped"}
}

func describePulseFinished(f *tapeFacts) tapeText {
	challenges, hasCh := numOf(f.d["challenges"])
	blocks, hasBl := numOf(f.d["blocks"])
	p50, hasP50 := numOf(f.d["p50"])
	pl := func(n float64, word string) string {
		if n == 1 {
			return jsnum.Count(n) + " " + word
		}
		return jsnum.Count(n) + " " + word + "s"
	}
	parts := []string{}
	if f.hasRequests {
		parts = append(parts, jsnum.Count(f.requests)+" requests")
	}
	if hasP50 {
		parts = append(parts, "p50 "+jsnum.Ms(p50))
	}
	if hasCh && challenges != 0 {
		parts = append(parts, pl(challenges, "challenge"))
	}
	if hasBl && blocks != 0 {
		parts = append(parts, pl(blocks, "block"))
	}
	value := jsnum.NA
	if f.hasRate {
		value = jsnum.Pct(f.rate, 0)
	}
	return tapeText{"Pulse" + f.inWhere, joinParts(parts...), value}
}

func describeSuccessDrop(f *tapeFacts) tapeText {
	if !f.hasRate {
		return tapeText{"Success dropped" + f.inWhere, f.baselineText, jsnum.NA}
	}
	return tapeText{"Success dropped to " + jsnum.Pct(f.rate, 0) + f.inWhere, f.baselineText, jsnum.Pct(f.rate, 0)}
}

func describeLatencySpike(f *tapeFacts) tapeText {
	p50, ok := numOf(f.d["p50"])
	if !ok {
		p50, ok = numOf(f.d["value"])
	}
	detail := ""
	if f.hasBaseline {
		detail = "baseline " + jsnum.Ms(f.baseline)
	}
	if !ok {
		return tapeText{"Latency spiked" + f.inWhere, detail, jsnum.NA}
	}
	return tapeText{"Latency spiked to " + jsnum.Ms(p50) + f.inWhere, detail, jsnum.Ms(p50)}
}

func describeBlockSpike(f *tapeFacts) tapeText {
	value := jsnum.NA
	if blocks, ok := numOf(f.d["blocks"]); ok {
		value = jsnum.Count(blocks) + " blocks"
	}
	return tapeText{"Blocks spiked" + f.inWhere, f.baselineText, value}
}

func describeDown(f *tapeFacts) tapeText {
	suffix := ""
	if s, _ := strOf(f.d["source"]); s == "census" {
		suffix = " (during census)"
	}
	return tapeText{"Down" + f.inWhere + suffix, "", "Down"}
}

func describeRecovered(f *tapeFacts) tapeText {
	if f.where != "" {
		return tapeText{"Recovered" + f.inWhere, "", "Recovered"}
	}
	return tapeText{"Recovered", "", "Recovered"}
}

func describeNewHostingASN(f *tapeFacts) tapeText {
	name, _ := strOf(f.d["name"])
	return tapeText{"DC ASN seen" + f.inWhere, name, "New"}
}

func describeIPOverlap(f *tapeFacts) tapeText {
	value := jsnum.NA
	if shared, ok := numOf(f.d["value"]); ok {
		value = jsnum.Pct(shared, 0)
	}
	return tapeText{"IP overlap seen" + f.inWhere, "", value}
}

var tapeCategory = map[string]string{"census": "census", "pulse": "pulse", "incident": "incident", "audit": "audit"}

// tapeEvents renames the raw event kinds no answer may print as they are: an
// hour's release reads as an hourly update (API-ESTIMATES-1); every other kind
// is published as it is.
var tapeEvents = map[string]string{"estimate_hour_released": "hour_released"}

func tapeEvent(kind string) string {
	if name, ok := tapeEvents[kind]; ok {
		return name
	}
	return kind
}

func tapeKind(e *store.EventJSON, products []TapeProduct) string {
	if _, suffix, ok := strings.Cut(e.Product, "/"); ok && model.IsProxyKind(suffix) {
		return suffix
	}
	for _, p := range products {
		if p.Provider == e.Provider {
			if model.IsProxyKind(p.Kind) {
				return p.Kind
			}
			break
		}
	}
	return "residential"
}

// TapeFromEvents ports tape.ts tapeFromEvents. Events arrive newest first.
// slugs maps framework provider ids to site slugs (catalog rows may rename).
func TapeFromEvents(events []store.EventJSON, products []TapeProduct, generatedAt string, slugs map[string]string) []model.TapeEvent {
	now := snap.Ms(generatedAt)
	names := map[string]string{}
	for _, p := range products {
		names[p.Provider] = p.Name // new Map(...) keeps the last entry per key
	}
	out := make([]model.TapeEvent, 0, len(events))
	for i := range events {
		e := &events[i]
		var data map[string]any
		_ = json.Unmarshal(e.Data, &data)
		if data == nil {
			data = map[string]any{}
		}
		text := describe(e, data)
		providerName, ok := names[e.Provider]
		if !ok {
			providerName = e.Provider
		}
		slug := e.Provider
		if s, ok := slugs[e.Provider]; ok {
			slug = s
		}
		kind, ok := tapeCategory[e.Category]
		if !ok {
			kind = "incident"
		}
		atMs := snap.Ms(e.At)
		ago := 0.0
		if !math.IsNaN(now) && !math.IsNaN(atMs) {
			ago = math.Max(0, jsnum.Round((now-atMs)/1000))
		}
		detail := providerName
		if text.detail != "" {
			if detail != "" {
				detail += " · " + text.detail
			} else {
				detail = text.detail
			}
		}
		at, severity := e.At, e.Severity
		out = append(out, model.TapeEvent{ID: "event-" + jsnum.ToFixed(float64(e.ID), 0), Kind: kind, Event: tapeEvent(e.Kind), ProviderSlug: slug,
			ProviderName: providerName, Product: tapeKind(e, products), Title: text.title, Detail: detail,
			Signal: Num(e.Signal), Value: text.value, AgoSeconds: Num(ago), At: &at, Source: model.SourceMeasured, NA: []string{},
			Country: e.Country, Region: e.Region, Target: e.Target, Severity: &severity, Values: eventValues(e.Kind, data)})
	}
	return out
}
