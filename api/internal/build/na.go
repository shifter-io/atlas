package build

import (
	"bytes"
	"encoding/json"
	"regexp"
	"sort"
	"strconv"
	"strings"
)

// Paths whose null is part of the contract rather than missing data
// (patterns: array positions written as [], product categories as *).
var contractNull = map[string]bool{
	"pricing.plans[].catalog.tier": true, // untiered pools have no tier selector
	"framework":                    true, // framework identity: null for an unlinked catalog row
	"overlap.likelyUpstream":       true, // no reseller signature (measured providers declare it N/A)
	"pricing.plans[].gb":           true, // per-IP plans have no GB allowance
	"pricing.plans[].ips":          true, // per-GB plans have no IP count
	"at":                           true, // tape: only measured events carry an ISO stamp
	"invoiceMonth":                 true, // reviews: only invoice-verified reviews have one
	"linkedRunId":                  true,
	"providerReply":                true,
	"rejectionReason":              true, // community runs: only rejected runs
	"overlapShare":                 true, // only rotating products
	"providerSlug":                 true, // changelog: fleet-wide entries have no provider
	"providerName":                 true,
	"delta":                        true,
	"group":                        true, // targets: only a group target's sources belong to a group
	"incidents[].endedAt":          true, // an ongoing incident has no end (its ongoing is true)

	// Page answers (answer_na.go): an observed span covering the range, no
	// reseller signature, the all-networks answer's profile.
	"observedDays":                    true,
	"market.observedDays":             true,
	"likelyUpstream":                  true,
	"insights.exclusivity[].upstream": true,
	"profile":                         true,
	// insights.valueAll's partial bill: null when the bill is complete or
	// no target has a cost (value_all.go).
	"partialCostPer1k": true,
	// The rankings' and network ranges' partial 8-target bill, the same.
	"partialCostPer1kAllTargets": true,
}

// Array fields whose elements are named by a key in N/A paths, so a gap reads
// "targets[google_search].delta24h" instead of a position.
var identityKey = map[string]string{"targets": "target", "sources": "target", "products": "kind"}

var bracket = regexp.MustCompile(`\[[^\]]*\]`)

// pattern normalises a concrete path for contract and reason lookups.
func pattern(path string) string {
	p := bracket.ReplaceAllString(path, "[]")
	if strings.HasPrefix(p, "productMeasurements.") {
		parts := strings.SplitN(p, ".", 3)
		if len(parts) == 3 {
			return parts[2]
		}
		return "productMeasurements.*"
	}
	return p
}

// Page answers (answer_na.go) list these contract nulls too, each with a
// reason (MEASURED-FIELDS-1), and name a plan in its paths by its index
// (indexKey; PRICING-CODES-1).
var (
	answerListed = map[string]bool{"pricing.plans[].ips": true, "pricing.plans[].gb": true,
		"pricing.plans[].catalog.tier": true, "observedDays": true}
	answerIDs = map[string]string{"targets": "target", "sources": "target", "products": "kind", "plans": indexKey}
)

// indexKey in an ids map names an array's elements by their index.
const indexKey = "#"

type naWalker struct {
	paths map[string]bool
	// emptyIsNA marks empty arrays as missing (measured providers: an empty
	// list there means "not collected", never "none").
	emptyIsNA bool
	skip      map[string]bool
	// listed are contract nulls listed all the same; ids name array elements
	// (identityKey when nil).
	listed map[string]bool
	ids    map[string]string
	// status is, when not nil, a successScore's status by the path of each
	// of its null score, rank and rankedOf: their reason.
	status map[string]string
}

func (w *naWalker) walk(v any, path, field string) {
	if w.skip[path] {
		return
	}
	switch x := v.(type) {
	case nil:
		if p := pattern(path); !contractNull[p] || w.listed[p] {
			w.paths[path] = true
		}
	case map[string]any:
		w.scoreStatus(x, path, field)
		for k, child := range x {
			if k == "na" || k == "naReason" || k == "naReasons" {
				continue
			}
			p := k
			if path != "" {
				p = path + "." + k
			}
			w.walk(child, p, k)
		}
	case []any:
		if len(x) == 0 && w.emptyIsNA {
			w.paths[path] = true
		}
		for i, child := range x {
			label, ids := "", w.ids
			if ids == nil {
				ids = identityKey
			}
			if key, ok := ids[field]; ok && key == indexKey {
				label = strconv.Itoa(i)
			} else if ok {
				if m, ok := child.(map[string]any); ok {
					if id, ok := m[key].(string); ok {
						label = id
					}
				}
			}
			w.walk(child, path+"["+label+"]", "")
		}
	}
}

// scoreStatus records a successScore's (or challengeScore's,
// CHALLENGE-OVERALL-1) status as the reason of its null score, rank and
// rankedOf (SUCCESS-SCORE-API-1).
func (w *naWalker) scoreStatus(x map[string]any, path, field string) {
	status, ok := x["status"].(string)
	if w.status == nil || field != "successScore" && field != "challengeScore" || !ok {
		return
	}
	for _, k := range [...]string{"score", "rank", "rankedOf"} {
		if v, has := x[k]; has && v == nil {
			w.status[path+"."+k] = status
		}
	}
}

// decode turns a value into generic JSON.
func decode(v any) (any, error) {
	b, err := json.Marshal(v)
	if err != nil {
		return nil, err
	}
	dec := json.NewDecoder(bytes.NewReader(b))
	dec.UseNumber()
	var out any
	err = dec.Decode(&out)
	return out, err
}

// lookup resolves a dotted path (no arrays) in generic JSON.
func lookup(v any, path string) (any, bool) {
	for _, seg := range strings.Split(path, ".") {
		m, ok := v.(map[string]any)
		if !ok {
			return nil, false
		}
		if v, ok = m[seg]; !ok {
			return nil, false
		}
	}
	return v, true
}

func emptyValue(v any) bool {
	switch x := v.(type) {
	case nil:
		return true
	case []any:
		return len(x) == 0
	case map[string]any:
		return len(x) == 0
	}
	return false
}

// naOf lists the N/A paths of one entity: every null leaf that is not null by
// contract, every declared path whose value is empty, and (emptyIsNA) every
// empty list.
func naOf(entity any, declared []string, emptyIsNA bool, skip ...string) ([]string, error) {
	return walkNA(&naWalker{paths: map[string]bool{}, emptyIsNA: emptyIsNA, skip: map[string]bool{}}, entity, declared, skip)
}

// answerNAOf is naOf of a page answer's entity: answerListed contract nulls
// listed, plans by index (answerIDs).
// status is each successScore's status by the paths it explains.
func answerNAOf(entity any, declared []string, skip ...string) (na []string, status map[string]string, err error) {
	w := &naWalker{paths: map[string]bool{}, skip: map[string]bool{}, listed: answerListed, ids: answerIDs, status: map[string]string{}}
	na, err = walkNA(w, entity, declared, skip)
	return na, w.status, err
}

func walkNA(w *naWalker, entity any, declared, skip []string) ([]string, error) {
	g, err := decode(entity)
	if err != nil {
		return nil, err
	}
	for _, s := range skip {
		w.skip[s] = true
	}
	w.walk(g, "", "")
	for _, d := range declared {
		if v, ok := lookup(g, d); ok && emptyValue(v) {
			w.paths[d] = true
		}
	}
	out := make([]string, 0, len(w.paths))
	for p := range w.paths {
		out = append(out, p)
	}
	sort.Strings(out)
	return out, nil
}

// withoutPublishedEmpties drops the N/A mark naOf gives a measured provider's
// empty incident list when its snapshot product publishes outages (v2): there
// an empty list means no outage, a real zero, not "not collected".
func withoutPublishedEmpties(e *entry) []string {
	published := map[string]bool{}
	for k, sp := range e.Snap {
		if sp != nil && sp.Outages != nil {
			published["productMeasurements."+k+".incidents"] = true
			if k == e.P.PrimaryKind {
				published["incidents"] = true
			}
		}
	}
	out := e.P.NA[:0]
	for _, p := range e.P.NA {
		if !published[p] {
			out = append(out, p)
		}
	}
	return out
}

// CoverageField is one N/A path with its reason code and the table or
// column that would fill it (na_reasons.go).
type CoverageField struct {
	Path   string  `json:"path"`
	Reason string  `json:"reason"`
	Source *string `json:"source"`
}

// CoverageEntity is the N/A report of one entity; only a network has a name
// (a target, scope or profile is named by its id).
type CoverageEntity struct {
	ID       string                     `json:"id"`
	Name     string                     `json:"name,omitempty"`
	Source   string                     `json:"source,omitempty"`
	NACount  int                        `json:"naCount"`
	Sections map[string][]CoverageField `json:"sections"`
}

// Coverage is /v1/coverage.
type Coverage struct {
	GeneratedAt string                      `json:"generatedAt"`
	BuiltAt     string                      `json:"builtAt"`
	Summary     CoverageSummary             `json:"summary"`
	Dataset     CoverageEntity              `json:"dataset"`
	Providers   []CoverageEntity            `json:"providers"`
	Targets     []CoverageEntity            `json:"targets"`
	Scopes      []CoverageEntity            `json:"scopes"`
	Profiles    []CoverageEntity            `json:"profiles"`
	Records     map[string]CoverageRecords  `json:"records"`
	Fields      map[string]CoverageFieldUse `json:"fields"`
}

// CoverageIndex is /v1/coverage: the report without the per-provider gap
// lists, which /v1/coverage/{slug} serves one provider at a time (the whole
// report grows with every network: 325 KB for the 21 providers of QA-1).
type CoverageIndex struct {
	GeneratedAt string                      `json:"generatedAt"`
	BuiltAt     string                      `json:"builtAt"`
	Summary     CoverageSummary             `json:"summary"`
	Dataset     CoverageEntity              `json:"dataset"`
	Providers   []CoverageEntry             `json:"providers"`
	Targets     []CoverageEntity            `json:"targets"`
	Scopes      []CoverageEntity            `json:"scopes"`
	Profiles    []CoverageEntity            `json:"profiles"`
	Records     map[string]CoverageRecords  `json:"records"`
	Fields      map[string]CoverageFieldUse `json:"fields"`
}

// CoverageEntry names one provider's report in the index.
type CoverageEntry struct {
	ID      string `json:"id"`
	Name    string `json:"name,omitempty"`
	Source  string `json:"source,omitempty"`
	NACount int    `json:"naCount"`
}

// Index is the report with each provider reduced to its entry.
func (c *Coverage) Index() *CoverageIndex {
	out := &CoverageIndex{GeneratedAt: c.GeneratedAt, BuiltAt: c.BuiltAt, Summary: c.Summary, Dataset: c.Dataset,
		Providers: make([]CoverageEntry, len(c.Providers)), Targets: c.Targets, Scopes: c.Scopes, Profiles: c.Profiles,
		Records: c.Records, Fields: c.Fields}
	for i, p := range c.Providers {
		out.Providers[i] = CoverageEntry{ID: p.ID, Name: p.Name, Source: p.Source, NACount: p.NACount}
	}
	return out
}

type CoverageSummary struct {
	Providers         int            `json:"providers"`
	ProvidersBySource map[string]int `json:"providersBySource"`
	NAFields          int            `json:"naFields"`
	NAFieldsBySource  map[string]int `json:"naFieldsBySource"`
	SnapshotPublished bool           `json:"snapshotPublished"`
}

// CoverageRecords summarises tape/changelog/reviews/community runs.
type CoverageRecords struct {
	Count   int            `json:"count"`
	NACount int            `json:"naCount"`
	Fields  map[string]int `json:"fields"`
}

// CoverageFieldUse counts, per field pattern, how many providers lack it.
type CoverageFieldUse struct {
	Providers []string `json:"providers"`
	Reason    string   `json:"reason"`
	Source    *string  `json:"source"`
}

func section(path string) string {
	p := bracket.ReplaceAllString(path, "")
	parts := strings.Split(p, ".")
	if parts[0] == "productMeasurements" && len(parts) >= 3 {
		return "productMeasurements." + parts[1] + "." + parts[2]
	}
	switch parts[0] {
	case "pool", "performance", "pricing", "trust", "targets", "series", "ipSamples", "incidents", "geo", "asns", "overlap", "score", "reviews", "productMeasurements", "products":
		return parts[0]
	}
	return "identity"
}

func coverageEntity(id, name, source string, na []string, sectioned bool, reason func(string) naReason) CoverageEntity {
	e := CoverageEntity{ID: id, Name: name, Source: source, NACount: len(na), Sections: map[string][]CoverageField{}}
	for _, p := range na {
		s := "fields"
		if sectioned {
			s = section(p)
		}
		r := reason(p)
		e.Sections[s] = append(e.Sections[s], CoverageField{Path: p, Reason: r.code, Source: r.sourceOrNull()})
	}
	return e
}
