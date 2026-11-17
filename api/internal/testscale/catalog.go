package testscale

import (
	"fmt"
	"strings"

	"github.com/Zeus-Development/atlas/api/internal/store"
)

// catalog is the site catalog's category and difficulty of every target a
// shape publishes (api/scripts/catalog-targets.sql). A deploy seeds them
// (`atlas db-seed --only targets`, SEED-ON-DEPLOY-1) and the build refuses a
// published group or browser target without them (build.ErrCatalogIncomplete).
var catalog = map[string]struct {
	category   string
	difficulty int16
}{
	"generic_http": {"baseline", 1}, "generic_https": {"baseline", 1}, "amazon_pdp": {"ecommerce", 2},
	"google_search": {"search", 4}, "linkedin_public": {"social", 5}, "instagram_web": {"social", 3},
	"x_web": {"social", 3}, "social_web": {"social", 3}, "cloudflare_challenge": {"anti_bot", 4}, GroupTarget: {"ai_answers", 5},
	"ai_overview": {"ai_answers", 5}, "ai_chatgpt": {"ai_answers", 5}, "ai_gemini": {"ai_answers", 5},
}

// Catalog is the catalog rows of the targets, in their order: the ones the
// catalog names, the others left out (an in-memory shape's site.targets).
func Catalog(targets []store.FrameworkTarget) []store.TargetRow {
	out := []store.TargetRow{}
	for _, t := range targets {
		c, ok := catalog[t.ID]
		if !ok {
			continue
		}
		category, difficulty := c.category, c.difficulty
		out = append(out, store.TargetRow{ID: t.ID, Category: &category, Difficulty: &difficulty, SortOrder: t.SortOrder})
	}
	return out
}

// catalogSQL gives the targets the catalog knows their site.targets row, as
// the deploy's seed does, once schema site exists; a row already there is kept.
func catalogSQL(targets []BrowserTarget) string {
	rows := []string{}
	for _, t := range targets {
		if c, ok := catalog[t.ID]; ok {
			rows = append(rows, fmt.Sprintf("('%s','%s',%d,%d)", t.ID, c.category, c.difficulty, t.Sort))
		}
	}
	return "DO $do$ BEGIN IF to_regclass('site.targets') IS NOT NULL THEN EXECUTE $q$" +
		"INSERT INTO site.targets(target_id,category,difficulty,sort_order) VALUES " + strings.Join(rows, ",") +
		" ON CONFLICT (target_id) DO NOTHING$q$; END IF; END $do$"
}
