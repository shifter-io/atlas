package server

import (
	"encoding/json"
	"strings"
	"testing"
)

// windowFigureKeys are the per-range figures every network answer's
// stats.<range> carries (WINDOW-FIGURES-1).
var windowFigureKeys = []string{"captchaRate", "blockRate", "requestSuccessRate", "countries", "regions", "cities", "asns",
	"datacenterAsnShare", "exclusiveShare", "sharedShare", "observedDays", "latencyUnderLoad"}

// windowFiguresServed checks a v4 build's answers: every network's
// stats.<range> carries each figure, a measured one (with a latency target)
// with a value, and every chart answer times each point of its histories.
func windowFiguresServed(t *testing.T, answers map[string][]byte) {
	t.Helper()
	networks := 0
	for path, body := range answers {
		switch {
		case strings.HasPrefix(path, "/v1/networks/") && strings.Count(path, "/") == 4 && !strings.HasPrefix(path, "/v1/networks/all/"):
			networks++
			networkFigures(t, path, body)
		case strings.HasPrefix(path, "/v1/charts/"):
			chartTimes(t, path, body)
		}
	}
	if networks == 0 {
		t.Error("no network answer")
	}
}

func networkFigures(t *testing.T, path string, body []byte) {
	t.Helper()
	var ans struct {
		Stats map[string]map[string]any `json:"stats"`
	}
	if err := json.Unmarshal(body, &ans); err != nil {
		t.Fatalf("%s: %v", path, err)
	}
	for rangeID, stats := range ans.Stats {
		measured := stats["latencyTarget"] != nil
		for _, k := range windowFigureKeys {
			v, ok := stats[k]
			if !ok {
				t.Errorf("%s %s: no %s", path, rangeID, k)
			}
			if measured && v == nil && k != "observedDays" {
				t.Errorf("%s %s: %s is null", path, rangeID, k)
			}
		}
	}
}

// chartTimes checks a chart answer: every point of every history has its
// time t, no history has English labels and each outage band is its
// severity and country, without a label (UNREAD-TEXT-1, #977).
func chartTimes(t *testing.T, path string, body []byte) {
	t.Helper()
	var c map[string]json.RawMessage
	if err := json.Unmarshal(body, &c); err != nil {
		t.Fatalf("%s: %v", path, err)
	}
	if _, ok := c["labels"]; ok {
		t.Errorf("%s serves labels", path)
	}
	for _, h := range []string{"success", "availability", "latency", "latencyUnderLoad", "pool"} {
		var points []map[string]any
		if err := json.Unmarshal(c[h], &points); err != nil || points == nil {
			t.Errorf("%s: %s is not a list (%v)", path, h, err)
		}
		for _, p := range points {
			if _, ok := p["t"].(float64); !ok {
				t.Errorf("%s: a %s point has no time: %v", path, h, p)
			}
		}
	}
	var bands []map[string]any
	if err := json.Unmarshal(c["bands"], &bands); err != nil {
		t.Errorf("%s bands: %v", path, err)
	}
	for _, b := range bands {
		_, severity := b["severity"]
		_, country := b["country"]
		if _, label := b["label"]; label || !severity || !country {
			t.Errorf("%s: band %v", path, b)
		}
	}
}
