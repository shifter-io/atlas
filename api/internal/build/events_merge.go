package build

import (
	"slices"

	"github.com/Zeus-Development/proxy-index/api/internal/store"
)

// mergeTarget is the target whose row a merged row shows, when it has one.
const mergeTarget = "generic_http"

// matching is f's rows among refs, one per incident (EVENTS-MERGE-1) unless
// f names a target (store.EventFilter.Merged). With after, a group whose
// newest id is past after also takes its rows at or below it, so a page
// boundary never splits a group.
func matching(f store.EventFilter, refs []eventRef) []eventRef {
	g := f
	if f.Merged() {
		g.After = nil
	}
	out := []eventRef{}
	for _, r := range refs {
		if g.Match(r.e) && r.j.hasWords(f.Words) {
			out = append(out, r)
		}
	}
	if !f.Merged() {
		return out
	}
	out = mergeRefs(out)
	if f.After != nil {
		out = slices.DeleteFunc(out, func(r eventRef) bool { return r.e.ID <= *f.After })
	}
	return out
}

// mergeRefs is refs with each group's merging rows (store.Event.Merges) as
// one row, where the group's first row stands.
func mergeRefs(refs []eventRef) []eventRef {
	at := map[store.GroupKey]int{}
	groups := make([][]eventRef, 0, len(refs))
	for _, r := range refs {
		if r.e.Merges() {
			k := r.e.Group()
			if i, ok := at[k]; ok {
				groups[i] = append(groups[i], r)
				continue
			}
			at[k] = len(groups)
		}
		groups = append(groups, []eventRef{r})
	}
	out := make([]eventRef, len(groups))
	for i, g := range groups {
		out[i] = merged(g)
	}
	return out
}

// merged is one group's row: its generic_http row, else its first written
// (smallest id), with the group's largest id, so an after cursor passes the
// whole group, and its targets, sorted.
func merged(g []eventRef) eventRef {
	if len(g) == 1 {
		return g[0]
	}
	rep, top, targets := g[0], g[0].e.ID, []string{}
	for _, r := range g {
		top = max(top, r.e.ID)
		if better(r.e, rep.e) {
			rep = r
		}
		for _, t := range r.j.Targets {
			if !slices.Contains(targets, t) {
				targets = append(targets, t)
			}
		}
	}
	slices.Sort(targets)
	e, j := *rep.e, *rep.j
	e.ID, j.ID, j.Targets = top, top, targets
	return eventRef{&e, &j}
}

// better reports whether a stands for its group before b.
func better(a, b *store.Event) bool {
	ah, bh := isTarget(a, mergeTarget), isTarget(b, mergeTarget)
	if ah != bh {
		return ah
	}
	return a.ID < b.ID
}

func isTarget(e *store.Event, target string) bool { return e.Target != nil && *e.Target == target }

// targetsOf is a row's own targets: its target, or none.
func targetsOf(e *store.EventJSON) []string {
	if e.Target == nil {
		return []string{}
	}
	return []string{*e.Target}
}
