package store

import (
	"cmp"
	"encoding/json"
	"fmt"
	"slices"
	"strings"
)

// The report of `siteapi explain` (Explain): explain.json is ExplainReport,
// explain.md its Markdown.

// Explained is one statement's EXPLAIN ANALYZE, or why it was not run.
type Explained struct {
	Name        string     `json:"name"`
	Site        string     `json:"call_site"`
	Only        string     `json:"layout,omitempty"`
	Skipped     string     `json:"skipped,omitempty"`
	Error       string     `json:"error,omitempty"`
	PlanningMs  float64    `json:"planning_ms"`
	ExecutionMs float64    `json:"execution_ms"`
	Rows        float64    `json:"rows"`
	SharedHit   int64      `json:"shared_hit"`
	SharedRead  int64      `json:"shared_read"`
	Nodes       []PlanNode `json:"top_nodes,omitempty"`
	SQL         string     `json:"sql"`
}

// Ms is the statement's planning and execution time.
func (e *Explained) Ms() float64 { return e.PlanningMs + e.ExecutionMs }

// PlanNode is one of a plan's top nodes: the root (depth 0) and its children.
type PlanNode struct {
	Depth    int     `json:"depth"`
	Type     string  `json:"type"`
	Relation string  `json:"relation,omitempty"`
	Index    string  `json:"index,omitempty"`
	Rows     float64 `json:"rows"`
	Loops    float64 `json:"loops"`
	TotalMs  float64 `json:"total_ms"`
}

// ExplainReport is every registered statement, in registry order.
type ExplainReport struct {
	Params     ExplainParams `json:"params"`
	Statements []Explained   `json:"statements"`
}

type planJSON struct {
	Type     string     `json:"Node Type"`
	Relation string     `json:"Relation Name"`
	Index    string     `json:"Index Name"`
	Rows     float64    `json:"Actual Rows"`
	Loops    float64    `json:"Actual Loops"`
	TotalMs  float64    `json:"Actual Total Time"`
	Hit      int64      `json:"Shared Hit Blocks"`
	Read     int64      `json:"Shared Read Blocks"`
	Plans    []planJSON `json:"Plans"`
}

// read fills e from EXPLAIN's JSON.
func (e *Explained) read(out []byte) error {
	var doc []struct {
		Plan        planJSON `json:"Plan"`
		PlanningMs  float64  `json:"Planning Time"`
		ExecutionMs float64  `json:"Execution Time"`
	}
	if err := json.Unmarshal(out, &doc); err != nil || len(doc) != 1 {
		return fmt.Errorf("EXPLAIN output: %v", err)
	}
	d := doc[0]
	e.PlanningMs, e.ExecutionMs, e.Rows, e.SharedHit, e.SharedRead = d.PlanningMs, d.ExecutionMs, d.Plan.Rows, d.Plan.Hit, d.Plan.Read
	node := func(depth int, n planJSON) PlanNode {
		return PlanNode{depth, n.Type, n.Relation, n.Index, n.Rows, n.Loops, n.TotalMs}
	}
	e.Nodes = []PlanNode{node(0, d.Plan)}
	for _, c := range d.Plan.Plans {
		e.Nodes = append(e.Nodes, node(1, c))
	}
	return nil
}

// FlagMs flags a statement in explain.md; TimeoutShare one over 10% of the
// API's statement timeout (OpenReadOnly).
const (
	FlagMs       = 100
	TimeoutShare = 30000 / 10
)

// Markdown is the report as explain.md: the timed statements slowest first,
// each over FlagMs or TimeoutShare flagged, then the skipped and failed ones.
func (r *ExplainReport) Markdown() []byte {
	var b strings.Builder
	p := r.Params
	fmt.Fprintf(&b, "# siteapi explain\n\nBuild %d (v4: %t); network list %d (%s, %d rows, largest dimension %q); %d statements.\n\n",
		p.Build, p.V4, p.List, p.Provider, p.ListRows, p.Dimension, len(r.Statements))
	timed := slices.DeleteFunc(slices.Clone(r.Statements), func(e Explained) bool { return e.Skipped != "" || e.Error != "" })
	slices.SortStableFunc(timed, func(a, b Explained) int { return cmp.Compare(b.Ms(), a.Ms()) })
	b.WriteString("| Statement | Call site | Total ms | Planning ms | Execution ms | Rows | Shared hit | Shared read | Flag |\n|---|---|--:|--:|--:|--:|--:|--:|---|\n")
	for _, e := range timed {
		flag := ""
		switch {
		case e.Ms() > TimeoutShare:
			flag = "**over 10% of the 30 s timeout**"
		case e.Ms() > FlagMs:
			flag = "**over 100 ms**"
		}
		fmt.Fprintf(&b, "| `%s` | `%s` | %.2f | %.2f | %.2f | %.0f | %d | %d | %s |\n", e.Name, e.Site, e.Ms(), e.PlanningMs, e.ExecutionMs, e.Rows, e.SharedHit, e.SharedRead, flag)
	}
	for _, kind := range []string{"Skipped", "Failed"} {
		lines := []string{}
		for _, e := range r.Statements {
			if why := map[string]string{"Skipped": e.Skipped, "Failed": e.Error}[kind]; why != "" {
				lines = append(lines, fmt.Sprintf("- `%s` (`%s`): %s\n", e.Name, e.Site, why))
			}
		}
		if len(lines) > 0 {
			b.WriteString("\n## " + kind + "\n\n" + strings.Join(lines, ""))
		}
	}
	return []byte(b.String())
}
