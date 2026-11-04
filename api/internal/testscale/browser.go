package testscale

import (
	"encoding/json"
	"fmt"
	"strings"
)

// BrowserTarget is one browser pulse target (contract K-BP1) as the
// framework seeds it into public.targets.
type BrowserTarget struct {
	ID, Label, URL string
	Sort           int32
	Group          string // the group target it is a source of, "" when none
}

// GroupTarget is the one group target; its sources are the targets whose
// Group names it, in BrowserTargets order.
const GroupTarget = "ai_answers"

// BrowserTargets is K-BP1's nine browser targets in sort order.
var BrowserTargets = []BrowserTarget{
	{"amazon_pdp", "Marketplace product page", "https://www.amazon.com/dp/", 30, ""},
	{"google_search", "Google Search SERP", "https://www.google.com/search", 40, ""},
	{"linkedin_public", "Public professional profile", "https://www.linkedin.com/in/", 50, ""},
	{"instagram_web", "Social media web app", "https://www.instagram.com/", 60, ""},
	{"cloudflare_challenge", "Managed challenge page", "https://challenges.cloudflare.com/", 70, ""},
	{GroupTarget, "AI answers", "https://www.google.com/search", 80, ""},
	{"ai_overview", "AI answers: Google AI Overview", "https://www.google.com/search", 81, GroupTarget},
	{"ai_chatgpt", "AI answers: ChatGPT", "https://chatgpt.com/", 82, GroupTarget},
	{"ai_gemini", "AI answers: Gemini", "https://gemini.google.com/app", 83, GroupTarget},
}

// GroupSources is the group target's sources in order.
func GroupSources() []string {
	out := []string{}
	for _, t := range BrowserTargets {
		if t.Group == GroupTarget {
			out = append(out, t.ID)
		}
	}
	return out
}

// BrowserTargetsSQL inserts BrowserTargets into public.targets the way the
// framework seeds them: engine browser, never in a census or a pulse.
func BrowserTargetsSQL() string {
	rows := make([]string, 0, len(BrowserTargets))
	for _, t := range BrowserTargets {
		rows = append(rows, fmt.Sprintf("('%s','%s','%s','browser',false,false,%d)", t.ID, t.Label, t.URL, t.Sort))
	}
	return "INSERT INTO targets(target_id,label,url,engine,census,pulse_enabled,sort_order) VALUES " + strings.Join(rows, ",")
}

// Outcomes is the K-BP6 outcome_rates keys in order.
var Outcomes = []string{"ok", "challenge", "block", "login_wall", "timeout", "proxy_error", "no_content", "no_answer", "other_error"}

// BrowserCounts is a browser target's tries per outcome (Outcomes order) in
// every window: distinct per target, 100 tries each. A group target's are
// the sum of its sources'.
func BrowserCounts(id string) []float64 {
	if id == GroupTarget {
		sum := make([]float64, len(Outcomes))
		for _, s := range GroupSources() {
			for i, n := range BrowserCounts(s) {
				sum[i] += n
			}
		}
		return sum
	}
	k := 0.0
	for i, t := range BrowserTargets {
		if t.ID == id {
			k = float64(i)
		}
	}
	// ok, challenge, block, login_wall, timeout, proxy_error, no_content, no_answer, other_error
	return []float64{60 - 2*k, 10, 5 + k, 4, 6, 5, 4, 3 + k, 3}
}

// PageLoad is a browser target's page-load p50 and p90 (ms).
func PageLoad(id string) (p50, p90 float64) {
	for i, t := range BrowserTargets {
		if t.ID == id {
			return 1000 + 100*float64(i), 2500 + 100*float64(i)
		}
	}
	return 0, 0
}

// AddBrowser adds every browser target to a snapshot document: each product
// window gets a K-BP6 entry (metrics from BrowserCounts, the browser object
// with outcome rates, page loads, group and sources) next to its network
// targets, the methodology its target_groups and the document's target list
// the nine targets. Network
// metrics are untouched, as the framework keeps browser runs out of them.
func AddBrowser(doc map[string]any) error {
	for _, p := range doc["products"].([]any) {
		for _, w := range p.(map[string]any)["windows"].(map[string]any) {
			targets := w.(map[string]any)["targets"].(map[string]any)
			template, ok := targets["generic_https"].(map[string]any)
			if !ok {
				return fmt.Errorf("a window has no generic_https target to copy")
			}
			for _, t := range BrowserTargets {
				entry, err := clone(template)
				if err != nil {
					return err
				}
				browserEntry(entry, t)
				targets[t.ID] = entry
			}
		}
	}
	if m, ok := doc["methodology"].(map[string]any); ok {
		if settings, ok := m["settings"].(map[string]any); ok {
			settings["target_groups"] = map[string]any{GroupTarget: GroupSources()}
		}
	}
	list, _ := doc["targets"].([]any)
	for _, t := range BrowserTargets {
		list = append(list, map[string]any{"id": t.ID, "url": t.URL, "label": t.Label, "version": "1", "engine": "browser"})
	}
	doc["targets"] = list
	return nil
}

func browserEntry(entry map[string]any, t BrowserTarget) {
	counts := BrowserCounts(t.ID)
	completed := 0.0
	for _, n := range counts {
		completed += n
	}
	rates := map[string]any{}
	for i, k := range Outcomes {
		rates[k] = counts[i] / completed
	}
	m := entry["metrics"].(map[string]any)
	m["completed"], m["successes"], m["errors"] = completed, counts[0], completed-counts[0]
	m["success_rate"], m["request_success_rate"] = counts[0]/completed, counts[0]/completed
	m["challenge_rate"], m["block_rate"], m["timeout_rate"] = counts[1]/completed, counts[2]/completed, counts[4]/completed
	p50, p90 := PageLoad(t.ID)
	var group, sources any
	if t.Group != "" {
		group = t.Group
	}
	if t.ID == GroupTarget {
		sources = GroupSources()
	}
	entry["browser"] = map[string]any{"outcome_rates": rates, "page_load_ms": map[string]any{"p50": p50, "p90": p90, "p95": p90 + 100, "p99": p90 + 200},
		"group": group, "sources": sources}
}

// WithBrowser is a snapshot payload with AddBrowser applied.
func WithBrowser(payload []byte) ([]byte, error) {
	var doc map[string]any
	if err := json.Unmarshal(payload, &doc); err != nil {
		return nil, err
	}
	if err := AddBrowser(doc); err != nil {
		return nil, err
	}
	return json.Marshal(doc)
}
