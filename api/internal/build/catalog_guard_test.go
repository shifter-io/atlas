package build

import (
	"errors"
	"strings"
	"testing"

	"github.com/shifter-io/atlas/api/internal/model"
	"github.com/shifter-io/atlas/api/internal/snap"
)

// The guard names every methodology target that lacks its row, category or
// difficulty, and passes a complete catalog or a build without a snapshot.
func TestCatalogCompleteNamesWhatIsMissing(t *testing.T) {
	s := &snap.Snapshot{}
	s.Methodology.Settings.TargetGroups = map[string][]string{"social_web": {"instagram_web", "x_web"}}
	s.Methodology.Settings.BrowserDifficulty = map[string]float64{"amazon_pdp": 2, "x_web": 3}
	row := func(id string) model.TargetMeta {
		return model.TargetMeta{ID: id, Category: model.S("social"), Difficulty: 3}
	}
	complete := []model.TargetMeta{row("social_web"), row("instagram_web"), row("x_web"), row("amazon_pdp")}
	if err := catalogComplete(complete, s); err != nil {
		t.Fatal(err)
	}
	if err := catalogComplete(nil, nil); err != nil {
		t.Fatalf("no snapshot: %v", err)
	}
	partial := []model.TargetMeta{row("instagram_web"), {ID: "x_web", Difficulty: 3}, {ID: "amazon_pdp", Category: model.S("ecommerce"), Difficulty: nan}}
	err := catalogComplete(partial, s)
	want := "amazon_pdp (no difficulty), social_web (no target row), x_web (no category)"
	if !errors.Is(err, ErrCatalogIncomplete) || !strings.Contains(err.Error(), want) {
		t.Fatalf("got %v, want %q", err, want)
	}
}
