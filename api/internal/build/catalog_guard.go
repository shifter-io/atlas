package build

import (
	"errors"
	"fmt"
	"sort"
	"strings"

	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/snap"
)

// The catalog guard (SEED-ON-DEPLOY-1, #1284). The published calculation's
// methodology names each group target and its sources ([target_groups]) and
// each browser target's difficulty ([browser_difficulty]). Each must be a
// target of the dataset with the catalog's category and difficulty
// (site.targets, which the deploy seeds with `atlas db-seed --only targets`).
// Otherwise the build fails, which the server refuses (server.Refused): the
// previous version keeps serving, /healthz is degraded with the reason (the
// server's alert), and a fresh colour never becomes healthy, so the deploy
// does not switch. A group published without its row, cells or card (staging,
// 16 Nov 2026: social_web) cannot be served.

// ErrCatalogIncomplete is the guard's refusal.
var ErrCatalogIncomplete = errors.New("the target catalog lacks the methodology's targets")

// catalogComplete checks the dataset's targets against the snapshot's
// methodology; nil without a published snapshot.
func catalogComplete(targets []model.TargetMeta, s *snap.Snapshot) error {
	if s == nil {
		return nil
	}
	need := map[string]bool{}
	for group, sources := range s.Methodology.Settings.TargetGroups {
		need[group] = true
		for _, id := range sources {
			need[id] = true
		}
	}
	for id := range s.Methodology.Settings.BrowserDifficulty {
		need[id] = true
	}
	have := map[string]model.TargetMeta{}
	for _, t := range targets {
		have[t.ID] = t
	}
	bad := []string{}
	for id := range need {
		t, ok := have[id]
		switch {
		case !ok:
			bad = append(bad, id+" (no target row)")
		case model.SV(t.Category) == "":
			bad = append(bad, id+" (no category)")
		case !t.Difficulty.OK():
			bad = append(bad, id+" (no difficulty)")
		}
	}
	if len(bad) == 0 {
		return nil
	}
	sort.Strings(bad)
	return fmt.Errorf("%w: %s; seed it with `atlas db-seed --only targets` (api/scripts/catalog-targets.sql)",
		ErrCatalogIncomplete, strings.Join(bad, ", "))
}
