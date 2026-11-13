package build

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"github.com/BurntSushi/toml"

	"github.com/Zeus-Development/proxy-index/api/internal/config"
)

// The scopes' cadence is the configuration's (#966): the production browser
// queue tries every (network, target) tries times every every_minutes, so
// tries x 1440 / every_minutes a day for each of its targets, and each
// network's formula schedules one UTC hour per country, so one daily test per
// network and country. A change to either fails here until the constants
// follow it.
func TestTheScopeCadenceIsTheConfigurations(t *testing.T) {
	t.Parallel()
	path := config.FindUp(filepath.Join("configs", "browser-pulse.production.example.toml"))
	var browser struct {
		Schedule struct {
			Plan string `toml:"plan"`
		} `toml:"schedule"`
		Target []struct {
			ID           string `toml:"id"`
			Tries        int    `toml:"tries"`
			EveryMinutes int    `toml:"every_minutes"`
		} `toml:"target"`
	}
	if _, err := toml.DecodeFile(path, &browser); err != nil || browser.Schedule.Plan != "queue" || len(browser.Target) == 0 {
		t.Fatalf("%s: %+v, %v", path, browser, err)
	}
	for _, tg := range browser.Target {
		if tg.Tries <= 0 || tg.EveryMinutes <= 0 || tg.Tries*24*60/tg.EveryMinutes != browserTriesPerTargetPerDay {
			t.Errorf("the browser queue tries %s %d times every %d minutes, the scopes say %d a day", tg.ID, tg.Tries, tg.EveryMinutes, browserTriesPerTargetPerDay)
		}
	}
	formulas, err := filepath.Glob(filepath.Join(filepath.Dir(config.FindUp(filepath.Join("formulas", "shifter.json"))), "*.json"))
	if err != nil || len(formulas) == 0 {
		t.Fatalf("no formulas: %v", err)
	}
	scheduled := 0
	for _, f := range formulas {
		raw, err := os.ReadFile(f)
		if err != nil {
			t.Fatal(err)
		}
		var formula struct {
			Schedule map[string]json.RawMessage `json:"schedule"`
		}
		if json.Unmarshal(raw, &formula) != nil || len(formula.Schedule) == 0 {
			continue // not a network's formula
		}
		scheduled++
		for country, hour := range formula.Schedule {
			var h int
			if err := json.Unmarshal(hour, &h); err != nil || dailyTestsPerNetworkCountryPerDay != 1 {
				t.Errorf("%s %s: %s is not one daily hour, the scopes say %d tests a day", filepath.Base(f), country, hour, dailyTestsPerNetworkCountryPerDay)
			}
		}
	}
	if scheduled == 0 {
		t.Error("no formula has a schedule")
	}
}
