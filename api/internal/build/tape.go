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

func describe(e *store.EventJSON, d map[string]any) tapeText {
	where := place(e, d)
	inWhere := ""
	if where != "" {
		inWhere = " in " + where
	}
	requests, hasRequests := numOf(d["requests"])
	seconds, hasSeconds := numOf(d["duration_s"])
	rate, hasRate := numOf(d["success_rate"])
	if !hasRate {
		rate, hasRate = numOf(d["value"])
	}
	baseline, hasBaseline := numOf(d["baseline"])
	baselineText := ""
	if hasBaseline {
		baselineText = "baseline " + jsnum.Pct(baseline, 0)
	}
	if t, ok := describeDaily(e, d, inWhere); ok {
		return t
	}
	if t, ok := describeOutage(e, d, inWhere); ok {
		return t
	}
	join := joinParts
	switch e.Kind {
	case "census_started":
		extra := ""
		if e.Country != nil && *e.Country != "" && e.Region != nil && *e.Region != "" {
			extra = " (" + *e.Region + ")"
		}
		target := ""
		if e.Target != nil {
			target = *e.Target
		}
		return tapeText{"Census started" + inWhere + extra, target, "Started"}
	case "census_finished":
		parts := []string{}
		if hasRequests {
			parts = append(parts, jsnum.Count(requests)+" requests")
		}
		if hasSeconds {
			parts = append(parts, runTime(seconds))
		}
		if s, _ := strOf(d["stop_reason"]); s == "stalled" {
			return tapeText{"Census stopped" + inWhere + ": network down", join(parts...), "Down"}
		}
		value := jsnum.NA
		if ips, ok := numOf(d["unique_ips"]); ok {
			value = jsnum.Count(ips) + " IPs"
		}
		return tapeText{"Census done" + inWhere, join(parts...), value}
	case "census_retry":
		// A census the provider spoiled (no success, stalled), run again once a pulse got through.
		detail := ""
		if reason, _ := strOf(d["reason"]); reason != "" {
			detail = "after " + strings.ReplaceAll(reason, "_", " ")
		}
		return tapeText{"Census rerun" + inWhere, detail, "Rerun"}
	case "census_skipped":
		reason, _ := strOf(d["reason"])
		return tapeText{"Census skipped" + inWhere, reason, "Skipped"}
	case "pulse_finished":
		challenges, hasCh := numOf(d["challenges"])
		blocks, hasBl := numOf(d["blocks"])
		p50, hasP50 := numOf(d["p50"])
		pl := func(n float64, word string) string {
			if n == 1 {
				return jsnum.Count(n) + " " + word
			}
			return jsnum.Count(n) + " " + word + "s"
		}
		parts := []string{}
		if hasRequests {
			parts = append(parts, jsnum.Count(requests)+" requests")
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
		if hasRate {
			value = jsnum.Pct(rate, 0)
		}
		return tapeText{"Pulse" + inWhere, join(parts...), value}
	case "success_drop":
		if !hasRate {
			return tapeText{"Success dropped" + inWhere, baselineText, jsnum.NA}
		}
		return tapeText{"Success dropped to " + jsnum.Pct(rate, 0) + inWhere, baselineText, jsnum.Pct(rate, 0)}
	case "latency_spike":
		p50, ok := numOf(d["p50"])
		if !ok {
			p50, ok = numOf(d["value"])
		}
		detail := ""
		if hasBaseline {
			detail = "baseline " + jsnum.Ms(baseline)
		}
		if !ok {
			return tapeText{"Latency spiked" + inWhere, detail, jsnum.NA}
		}
		return tapeText{"Latency spiked to " + jsnum.Ms(p50) + inWhere, detail, jsnum.Ms(p50)}
	case "block_spike":
		value := jsnum.NA
		if blocks, ok := numOf(d["blocks"]); ok {
			value = jsnum.Count(blocks) + " blocks"
		}
		return tapeText{"Blocks spiked" + inWhere, baselineText, value}
	case "down":
		suffix := ""
		if s, _ := strOf(d["source"]); s == "census" {
			suffix = " (during census)"
		}
		return tapeText{"Down" + inWhere + suffix, "", "Down"}
	case "recovered":
		if where != "" {
			return tapeText{"Recovered" + inWhere, "", "Recovered"}
		}
		return tapeText{"Recovered", "", "Recovered"}
	case "new_hosting_asn":
		name, _ := strOf(d["name"])
		return tapeText{"DC ASN seen" + inWhere, name, "New"}
	case "ip_overlap":
		value := jsnum.NA
		if shared, ok := numOf(d["value"]); ok {
			value = jsnum.Pct(shared, 0)
		}
		return tapeText{"IP overlap seen" + inWhere, "", value}
	case "node_offline":
		return tapeText{"Node offline" + inWhere, "", "Offline"}
	case "no_node":
		return tapeText{"No node available" + inWhere, "", "No node"}
	}
	return tapeText{strings.ReplaceAll(e.Kind, "_", " "), "", jsnum.NA}
}

var tapeCategory = map[string]string{"census": "census", "pulse": "pulse", "incident": "incident", "audit": "audit"}

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
		at := e.At
		out = append(out, model.TapeEvent{ID: "event-" + jsnum.ToFixed(float64(e.ID), 0), Kind: kind, ProviderSlug: slug,
			ProviderName: providerName, Product: tapeKind(e, products), Title: text.title, Detail: detail,
			Signal: Num(e.Signal), Value: text.value, AgoSeconds: Num(ago), At: &at, Source: model.SourceMeasured, NA: []string{}})
	}
	return out
}
