package store

import "testing"

// appliedChecksums is every embedded migration's checksum as the databases
// have it in site.schema_versions. A failure here means an applied migration
// file changed, which stops every existing database from being served: put the
// change in a new migration instead.
var appliedChecksums = map[string]string{
	"0001_site_schema.sql":  "5f15ada67e7d78d30216fc40827b995dae37810526100a935c8327e953788de0",
	"0002_plan_catalog.sql": "b0547ed89c51aa4e7c83a8ed42f09d6021e225087f5ad174b84deedf46026091",
}

func TestAppliedMigrationsAreNeverEdited(t *testing.T) {
	t.Parallel()
	migrations, err := Migrations()
	if err != nil {
		t.Fatal(err)
	}
	for _, m := range migrations {
		want, ok := appliedChecksums[m.Name]
		switch {
		case !ok:
			t.Errorf("new migration %s: add its checksum %s to appliedChecksums", m.Name, m.Checksum)
		case m.Checksum != want:
			t.Errorf("%s changed after it was applied (checksum %s, databases have %s): revert it and write a new migration", m.Name, m.Checksum, want)
		}
	}
	for name := range commentOnlyEdits {
		if _, ok := appliedChecksums[name]; !ok {
			t.Errorf("commentOnlyEdits names %s, which is not a migration", name)
		}
	}
}

func TestEarlierCommentOnlyChecksumsHaveTheSameSchema(t *testing.T) {
	t.Parallel()
	migrations, err := Migrations()
	if err != nil {
		t.Fatal(err)
	}
	m := migrations[0]
	if m.Name != "0001_site_schema.sql" {
		t.Fatalf("first migration is %s", m.Name)
	}
	// What siteapi builds with the earlier module paths wrote.
	for _, sum := range []string{m.Checksum, "8127cc64a2088bbd3667a4a55dee6f66e79b1d0c271354469aadc8e9c84741ec",
		"78404cdaea81a0e58379d7ce47132b8b098f0f6df9c091ce50c7915b57ae8916",
		"38700174a7bea7a14b1b8e72351e60ddd010f67987ffbff8ff4871954175f469"} {
		if !m.SameSchema(sum) {
			t.Errorf("%s with checksum %s refused", m.Name, sum)
		}
	}
	if m.SameSchema("tampered") || migrations[1].SameSchema("8127cc64a2088bbd3667a4a55dee6f66e79b1d0c271354469aadc8e9c84741ec") {
		t.Error("an unknown checksum was accepted")
	}
}
