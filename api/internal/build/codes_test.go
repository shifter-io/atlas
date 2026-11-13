package build

import (
	"fmt"
	"os"
	"strings"
	"testing"

	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/store"
)

// codesDoc is api/CODES.md, written from the registry.
const codesDoc = "../../CODES.md"

// TestCodesDocIsCurrent fails when api/CODES.md is not what the registry
// writes; GOLDEN_WRITE=1 rewrites it.
func TestCodesDocIsCurrent(t *testing.T) {
	t.Parallel()
	want := writeCodesDoc(CodeSets())
	if os.Getenv("GOLDEN_WRITE") != "" {
		if err := os.WriteFile(codesDoc, []byte(want), 0o644); err != nil {
			t.Fatal(err)
		}
		return
	}
	got, err := os.ReadFile(codesDoc)
	if err != nil || string(got) != want {
		t.Errorf("api/CODES.md is not the registry's (%v): run this test with GOLDEN_WRITE=1", err)
	}
}

// TestCodeParamsAreTyped checks every parameter's unit is a unit code and
// every code:<set> type names a registered set.
func TestCodeParamsAreTyped(t *testing.T) {
	t.Parallel()
	units := map[string]bool{}
	for _, c := range codeSets["unit"].Values {
		units[c.Code] = true
	}
	for _, s := range CodeSets() {
		for _, c := range s.Values {
			for _, p := range c.Params {
				if p.Unit != nil && !units[*p.Unit] {
					t.Errorf("%s.%s.%s: unit %q is no unit code", s.Name, c.Code, p.Name, *p.Unit)
				}
				if set, ok := strings.CutPrefix(p.Type, "code:"); ok && codeSets[set].Name == "" {
					t.Errorf("%s.%s.%s: %s names no set", s.Name, c.Code, p.Name, p.Type)
				}
			}
		}
	}
}

func TestRegisterCodeSetRefuses(t *testing.T) {
	t.Parallel()
	one := codes("a", "A.")
	for name, s := range map[string]CodeSet{
		"repeated set":   {Name: "unit", Source: CodesFromAPI, UsedIn: []string{"x y"}, Values: one},
		"bad name":       {Name: "Bad", Source: CodesFromAPI, UsedIn: []string{"x y"}, Values: one},
		"used nowhere":   {Name: "t_nowhere", Source: CodesFromAPI, Values: one},
		"no codes":       {Name: "t_empty", Source: CodesFromAPI, UsedIn: []string{"x y"}},
		"no catalog":     {Name: "t_catalog", Source: CodesFromCatalog, UsedIn: []string{"x y"}},
		"bad source":     {Name: "t_source", Source: "db", UsedIn: []string{"x y"}, Values: one},
		"repeated code":  {Name: "t_twice", Source: CodesFromAPI, UsedIn: []string{"x y"}, Values: codes("a", "A.", "a", "A.")},
		"no sentence":    {Name: "t_sentence", Source: CodesFromAPI, UsedIn: []string{"x y"}, Values: codes("a", "")},
		"unknown type":   {Name: "t_type", Source: CodesFromAPI, UsedIn: []string{"x y"}, Values: []Code{withParams("a", "A.", param("n", "text", ""))}},
		"catalog values": {Name: "t_both", Source: CodesFromCatalog, UsedIn: []string{"x y"}, Values: one, From: "x", Catalog: targetCodes},
	} {
		t.Run(name, func(t *testing.T) {
			defer func() {
				if recover() == nil {
					t.Errorf("RegisterCodeSet took %s", name)
				}
			}()
			RegisterCodeSet(s)
		})
	}
}

func TestCodesAnswer(t *testing.T) {
	t.Parallel()
	if _, err := buildCodes(&Context{}); err == nil {
		t.Error("no error without a dataset")
	}
	ds := &model.Dataset{Targets: []model.TargetMeta{{ID: "amazon_pdp", Label: model.S("Product page"), PassCondition: model.S("The price shows")}},
		Scopes:   []model.TestScope{{ID: "mobile"}},
		Profiles: []model.RankingProfile{{ID: "balanced", Label: model.S("Balanced")}, {ID: "cost-first"}}}
	in := &store.Inputs{Profiles: []store.ProfileRow{{ID: "balanced", Description: model.S("Every component")}}}
	bodies, err := buildCodes(&Context{GeneratedAt: "2026-11-13T00:00:00Z", Dataset: ds, Inputs: in})
	if err != nil {
		t.Fatal(err)
	}
	a := bodies[codesPath].(CodesAnswer)
	if a.Contract != Contract || a.Currency != "USD" || a.GeneratedAt != "2026-11-13T00:00:00Z" || len(a.Sets) != len(codeSets) {
		t.Errorf("answer %+v", a)
	}
	want := map[string]string{"target": "amazon_pdp=Product page: The price shows",
		"product_kind":    "mobile=No reference text in the catalog.",
		"ranking_profile": "balanced=Balanced: Every component|cost-first=No reference text in the catalog."}
	for _, s := range a.Sets {
		if w, ok := want[s.Name]; ok {
			got := []string{}
			for _, c := range s.Values {
				got = append(got, c.Code+"="+c.En)
			}
			if strings.Join(got, "|") != w {
				t.Errorf("%s: %v, want %s", s.Name, got, w)
			}
		}
	}
	bare, err := buildCodes(&Context{Dataset: &model.Dataset{}})
	if err != nil {
		t.Fatal(err)
	}
	for _, s := range bare[codesPath].(CodesAnswer).Sets {
		if s.Name == "ranking_profile" && (len(s.Values) != 1 || s.Values[0].Code != RankingProfile) {
			t.Errorf("without profile rows: %v, want the rankings' own", s.Values)
		}
	}
	if codeSets["target"].Values != nil {
		t.Error("the build changed the registry")
	}
}

// writeCodesDoc is api/CODES.md: the conventions, then every set.
func writeCodesDoc(sets []CodeSet) string {
	b := &strings.Builder{}
	b.WriteString(strings.ReplaceAll(codesDocHead, "{contract}", Contract))
	for _, s := range sets {
		fmt.Fprintf(b, "\n### `%s`\n\nUsed in: `%s`\n\n", s.Name, strings.Join(s.UsedIn, "`, `"))
		if s.Source == CodesFromCatalog {
			fmt.Fprintf(b, "From the build's catalog (%s): its codes are listed in `GET /v1/codes`.\n", s.From)
			continue
		}
		b.WriteString("| Code | Parameters | Meaning |\n|---|---|---|\n")
		for _, c := range s.Values {
			params := []string{}
			for _, p := range c.Params {
				unit := ""
				if p.Unit != nil {
					unit = ", " + *p.Unit
				}
				params = append(params, fmt.Sprintf("`%s` (%s%s)", p.Name, p.Type, unit))
			}
			fmt.Fprintf(b, "| `%s` | %s | %s |\n", c.Code, strings.Join(params, ", "), c.En)
		}
	}
	return b.String()
}

const codesDocHead = `# Codes

Written from the codes registry (` + "`api/internal/build/codes_*.go`" + `) by ` + "`TestCodesDocIsCurrent`" + `;
do not edit it by hand: change the registry and run that test with ` + "`GOLDEN_WRITE=1`" + `.

Every code the API sends, where it appears, its parameters and one English sentence for
documentation only. ` + "`GET /v1/codes`" + ` serves the same list, with the catalog sets' codes of the
current build, so a site in any language can translate every code. Contract: **{contract}**, the
date of the last breaking change (` + "`/v1/version`" + ` and ` + "`/v1/codes`" + ` ` + "`contract`" + `;
what changed on each date: ` + "`api/CHANGES.md`" + `).

## Conventions

- An answer holds only ids, slugs, codes, raw numbers, ISO 8601 times, ISO country codes, URLs,
  booleans and null: never a sentence, a written-out number, a plural or a relative time.
- Names stay names: networks, the provider's own plan, tier and pool names, place and
  network-operator names, and what users wrote in their reviews.
- Keys are camelCase; codes are lowercase snake_case; ids and slugs of catalog rows may be
  kebab-case (` + "`cost-first`" + `).
- Every field is present; a missing value is null, listed in its entity's ` + "`na`" + ` with a reason
  code (` + "`naReason`, `naReasons`" + `).
- ` + "`*Rate` and `*Share`" + ` are ratios from 0 to 1, ` + "`*Pct`" + ` percents from 0 to 100, ` + "`*Pts`" + `
  percentage points; new fields use ratios.
- A duration carries its unit in its name; the series times ` + "`t`, `trendT` and `times`" + ` are minutes
  before ` + "`generatedAt`" + `.
- Money is a plain number in US dollars (` + "`currency`" + ` in ` + "`/v1/codes`" + `).
- Times are RFC 3339 in UTC with ` + "`Z`" + ` and at most milliseconds; dates without a time are ` + "`YYYY-MM-DD`" + `.
- A message is ` + "`{code, params}`" + ` (an error is ` + "`{\"error\": {code, params}}`" + `); a record (a tape
  row, an event, a changelog entry) is ` + "`kind`" + ` (its group), ` + "`event`" + ` (its code) and
  ` + "`values`" + ` (its parameters).

## How to read a set

` + "`Used in`" + ` lists the fields that hold the set's codes: a route (` + "`*`" + ` for every answer and
the dataset file), then a path in the body. ` + "`[]`" + ` is every element of a list, ` + "`<id>`" + ` any key
of an object, ` + "`<key>`" + ` the keys themselves, and ` + "`**`" + ` any depth (never inside ` + "`naReasons`" + `, whose keys are
paths, or the N/A report's ` + "`sections`" + `, whose keys are section names). A parameter's type is one
of ` + "`number`, `integer`, `ratio`, `points`, `ms`, `seconds`, `minutes`, `usd`, `usd_per_gb`, `gb`,\n`mb`, `count`, `country`, `country_list`, `code:<set>`, `timestamp`, `date`, `name`,\n`string`, `string_list`, `path` (an error's echo of the request)" + `; its unit,
when the type does not say it, is a code of the ` + "`unit`" + ` set. A catalog set's codes are the
rows of the build's catalog, so only ` + "`GET /v1/codes`" + ` lists them.

## Sets
`
