package testscale

import (
	"encoding/json"
	"regexp"
	"strings"

	"github.com/Zeus-Development/atlas/api/internal/store"
)

// schemaVersion is the snapshot's schema version in an answer.
var schemaVersion = regexp.MustCompile(`"schemaVersion":\d+`)

// AsV4Answers is the answers of a build before payload v4 with the
// documented differences of the v4 build of the same data (NetworkHead: one
// network per product group) applied: each group's id and cohort are its
// network's, and the snapshot's schema version is 4. groups are the build's
// product groups, as its payload or its parts hold them.
func AsV4Answers(answers map[string][]byte, groups [][]byte) (map[string][]byte, error) {
	var ids, cohorts []string
	for _, raw := range groups {
		var g struct{ ID, Provider, Kind, Cohort string }
		if err := json.Unmarshal(raw, &g); err != nil {
			return nil, err
		}
		ids = append(ids, g.ID, NetworkID(g.Provider, g.Kind))
		cohorts = append(cohorts, g.Cohort, store.NetworkCohort)
	}
	// A group id holds its cohort: the ids go first.
	id, cohort := strings.NewReplacer(ids...), strings.NewReplacer(cohorts...)
	out := make(map[string][]byte, len(answers))
	for name, b := range answers {
		s := cohort.Replace(id.Replace(string(b)))
		out[name] = schemaVersion.ReplaceAll([]byte(s), []byte(`"schemaVersion":4`))
	}
	return out, nil
}
