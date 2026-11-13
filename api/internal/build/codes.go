package build

import (
	"errors"
	"fmt"
	"net/http"
	"regexp"
	"sort"
	"strings"
)

// /v1/codes (api/answers/codes.md, CODES-CATALOG-1): every code set the API
// sends, each code with its parameters and one English sentence for
// documentation only. Each area registers its sets from an init() in its
// own codes_<area>.go, as the answers register themselves, so a task adds
// its codes without editing a shared file. api/CODES.md is written from the
// same registry.

// Contract is the date of the API's last breaking change: /v1/version and
// /v1/codes serve it, and api/CHANGES.md lists what changed on each date.
const Contract = "2026-11-13"

const codesPath = "/v1/codes"

// The sources of a code set: the API's own codes, or the rows of the build's
// catalog (site.targets, site.scopes, site.ranking_profiles).
const (
	CodesFromAPI     = "api"
	CodesFromCatalog = "catalog"
)

// CodeParam is one named parameter of a code: its type and, where the type
// does not say it, its unit (a code of the unit set; null otherwise).
type CodeParam struct {
	Name string  `json:"name"`
	Type string  `json:"type"`
	Unit *string `json:"unit"`
}

// Code is one code, its English reference sentence and its parameters ([]
// for a plain code).
type Code struct {
	Code   string      `json:"code"`
	En     string      `json:"en"`
	Params []CodeParam `json:"params"`
}

// CodeSet is a set of codes and the field paths that hold them
// ("<route> <path>", [] for a list's elements, <id> for an object's keys).
type CodeSet struct {
	Name   string   `json:"name"`
	UsedIn []string `json:"usedIn"`
	Source string   `json:"source"`
	Values []Code   `json:"values"`
	// Catalog, for a catalog set, is its codes in one build, and From the
	// catalog table and columns they come from (api/CODES.md).
	Catalog func(*Context) []Code `json:"-"`
	From    string                `json:"-"`
}

// paramTypes are a parameter's types besides code:<set>.
var paramTypes = setOf("number", "integer", "ratio", "points", "ms", "seconds", "minutes", "usd", "usd_per_gb", "gb", "mb",
	"count", "country", "country_list", "timestamp", "date", "name", "string", "string_list", "path")

var codeSetName = regexp.MustCompile(`^[a-z][a-z0-9_]{0,40}$`)

var codeSets = map[string]CodeSet{}

// RegisterCodeSet adds a code set. Like RegisterAnswer, it panics on a
// programming error (a bad or repeated name, a repeated code, a parameter of
// an unknown type, an api set without codes or a catalog set without its
// Catalog); call it from an init() only.
func RegisterCodeSet(s CodeSet) {
	switch {
	case !codeSetName.MatchString(s.Name) || codeSets[s.Name].Name != "":
		panic(fmt.Sprintf("build: code set %q is invalid or registered twice", s.Name))
	case len(s.UsedIn) == 0:
		panic(fmt.Sprintf("build: code set %q is used nowhere", s.Name))
	case s.Source == CodesFromAPI && (len(s.Values) == 0 || s.Catalog != nil),
		s.Source == CodesFromCatalog && (len(s.Values) > 0 || s.Catalog == nil || s.From == ""),
		s.Source != CodesFromAPI && s.Source != CodesFromCatalog:
		panic(fmt.Sprintf("build: code set %q needs its codes (api) or a Catalog and From (catalog)", s.Name))
	}
	seen := map[string]bool{}
	for _, c := range s.Values {
		if c.Code == "" || seen[c.Code] || c.En == "" {
			panic(fmt.Sprintf("build: code set %q: code %q is empty, repeated or without its sentence", s.Name, c.Code))
		}
		seen[c.Code] = true
		for _, p := range c.Params {
			if !paramTypes[p.Type] && !strings.HasPrefix(p.Type, "code:") {
				panic(fmt.Sprintf("build: code set %q: %s.%s has an unknown type %q", s.Name, c.Code, p.Name, p.Type))
			}
		}
	}
	codeSets[s.Name] = s
}

// CodeSets is every registered set, by name; a catalog set without its
// codes.
func CodeSets() []CodeSet {
	out := make([]CodeSet, 0, len(codeSets))
	for _, s := range codeSets {
		out = append(out, s)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out
}

// CodesAnswer is /v1/codes.
type CodesAnswer struct {
	Contract    string    `json:"contract"`
	GeneratedAt string    `json:"generatedAt"`
	Currency    string    `json:"currency"`
	Sets        []CodeSet `json:"sets"`
}

func init() {
	RegisterAnswer(PageAnswer{Name: "codes", Route: "GET " + codesPath, Build: buildCodes, Samples: []string{codesPath},
		Key: func(r *http.Request) (string, error) {
			_, err := Query(r)
			return codesPath, err
		}})
}

// buildCodes is every set, each catalog set with this build's catalog rows.
func buildCodes(c *Context) (map[string]any, error) {
	if c.Dataset == nil {
		return nil, errors.New("no dataset")
	}
	sets := CodeSets()
	for i, s := range sets {
		if s.Catalog != nil {
			sets[i].Values = s.Catalog(c)
		}
	}
	return map[string]any{codesPath: CodesAnswer{Contract: Contract, GeneratedAt: c.GeneratedAt, Currency: "USD", Sets: sets}}, nil
}

// codes is plain codes from code, sentence pairs.
func codes(pairs ...string) []Code {
	out := make([]Code, 0, len(pairs)/2)
	for i := 0; i+1 < len(pairs); i += 2 {
		out = append(out, Code{Code: pairs[i], En: pairs[i+1], Params: []CodeParam{}})
	}
	return out
}

// withParams is a code with its parameters.
func withParams(code, en string, params ...CodeParam) Code {
	if params == nil {
		params = []CodeParam{}
	}
	return Code{Code: code, En: en, Params: params}
}

// param is a parameter; unit "" is null.
func param(name, typ, unit string) CodeParam {
	p := CodeParam{Name: name, Type: typ}
	if unit != "" {
		p.Unit = &unit
	}
	return p
}
