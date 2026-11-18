package store_test

import (
	"testing"

	"github.com/shifter-io/atlas/api/internal/store"
)

// A page answer's statement, registered as its own file would; the explain
// tests of this package time it with the others.
func init() {
	store.RegisterStatement(store.Statement{Name: "answer.registered", Site: "registerTest", Query: func(*store.ExplainParams) (string, []any) {
		return "SELECT 1 AS registered", nil
	}})
}

func TestARegisteredStatementFollowsTheOwnOnes(t *testing.T) {
	t.Parallel()
	all := store.Statements()
	if len(all) < 2 || all[len(all)-1].Name != "answer.registered" || all[0].Name != "poll.site_layout" {
		t.Fatalf("statements %d, first %q, last %q", len(all), all[0].Name, all[len(all)-1].Name)
	}
	if sql, _ := all[len(all)-1].Query(&store.ExplainParams{}); sql != "SELECT 1 AS registered" {
		t.Fatalf("registered SQL %q", sql)
	}
}
