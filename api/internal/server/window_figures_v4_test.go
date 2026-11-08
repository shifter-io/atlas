package server

import (
	"encoding/json"
	"strings"
	"testing"
)

// windowFigureKeys are the per-range figures every network answer's
// stats.<range> carries (WINDOW-FIGURES-1).
var windowFigureKeys = []string{"captchaRate", "blockRate", "requestSuccessRate", "countries", "regions", "cities", "asns",
	"datacenterAsnShare", "exclusiveShare", "sharedShare", "observedDays", "latencyUnderLoad", "latencyNote", "wireGbPer1k"}

// windowFiguresServed checks a v4 build's answers: every network's
// stats.<range> carries each figure, a measured one (with a latency target)
// with a value, and every chart answer labels each point of its histories.
func windowFiguresServed(t *testing.T, answers map[string][]byte) {
	t.Helper()
	networks := 0
	for path, body := range answers {
		switch {
		case strings.HasPrefix(path, "/v1/networks/") && strings.Count(path, "/") == 4 && !strings.HasPrefix(path, "/v1/networks/all/"):
			networks++
			networkFigures(t, path, body)
		case strings.HasPrefix(path, "/v1/charts/"):
			chartLabels(t, path, body)
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

func chartLabels(t *testing.T, path string, body []byte) {
	t.Helper()
	var c map[string]json.RawMessage
	if err := json.Unmarshal(body, &c); err != nil {
		t.Fatalf("%s: %v", path, err)
	}
	var labels map[string][]string
	if err := json.Unmarshal(c["labels"], &labels); err != nil {
		t.Fatalf("%s labels: %v", path, err)
	}
	for _, h := range []string{"success", "availability", "latency", "latencyUnderLoad", "pool"} {
		var points []json.RawMessage
		if err := json.Unmarshal(c[h], &points); err != nil || points == nil || len(labels[h]) != len(points) {
			t.Errorf("%s: %d %s labels for %d points (%v)", path, len(labels[h]), h, len(points), err)
		}
	}
}
