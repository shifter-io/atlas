package build

import (
	"testing"

	"github.com/shifter-io/atlas/api/internal/model"
	"github.com/shifter-io/atlas/api/internal/testscale"
)

// registered is the codes of a registered set.
func registered(t *testing.T, name string) map[string]bool {
	t.Helper()
	out := map[string]bool{}
	for _, c := range codeSets[name].Values {
		out[c.Code] = true
	}
	if len(out) == 0 {
		t.Fatalf("no code set %s", name)
	}
	return out
}

// checkReason fails t when r's code is not an N/A reason code or its source
// not a registered source.
func checkReason(t *testing.T, where string, r naReason, codes, sources map[string]bool) {
	t.Helper()
	if !codes[r.code] || (r.source != "" && !sources[r.source]) {
		t.Errorf("%s: reason %q, source %q is not registered", where, r.code, r.source)
	}
}

// Every rule of the reason table names a registered code and source, and
// every registered source is some rule's.
func TestEveryCoverageReasonIsRegistered(t *testing.T) {
	t.Parallel()
	codes, sources := registered(t, "na_reason"), registered(t, "na_source")
	used := map[string]bool{}
	all := []naReason{unknownGap, scopeReason(model.TestScope{NAReason: NACatalog})("overlapAudit")}
	for p, r := range reasons {
		checkReason(t, p, r, codes, sources)
		all = append(all, r)
	}
	for _, rp := range reasonPrefixes {
		checkReason(t, rp.prefix, rp.reason, codes, sources)
		all = append(all, rp.reason)
	}
	for _, r := range all {
		used[r.source] = true
	}
	for s := range sources {
		if !used[s] {
			t.Errorf("source %s is registered but no gap names it", s)
		}
	}
}

func TestReasonFor(t *testing.T) {
	t.Parallel()
	for path, want := range map[string]naReason{
		"hqCountry":                    {NACatalog, "site.providers.hq_country"},
		"targets[ai_answers].delta24h": {NANotPublished, srcSnapshot},
		"productMeasurements.mobile.trust.kycVerified": {NACatalog, "site.trust_signals.kyc_verified"},
		"targets[amazon_pdp].outcomes.ok":              notMeasuredSnap,
		"weights.speed":                                {NACatalog, "site.score_weights"},
		"score.breakdown.value":                        insufficientSnap,
		"ipSamples":                                    {NANotPublished, ""},
		"no.such.field":                                unknownGap,
		"pricing.plans[].name":                         {NANotNamedByProvider, "site.pricing_plans"},
		"productMeasurements.mobile.pricing.plans[].catalog.pool": {NANotNamedByProvider, "site.pricing_plans"},
	} {
		if got := reasonFor(path); got != want {
			t.Errorf("reasonFor(%s) = %v, want %v", path, got, want)
		}
	}
	scope := scopeReason(model.TestScope{NAReason: NACatalog, NAReasons: map[string]string{"cadence.pulseIntervalMinutes": NANoPulseFleet}})
	if got := scope("cadence.pulseIntervalMinutes"); got != (naReason{NANoPulseFleet, srcSnapshot}) {
		t.Errorf("a scope's pulse interval: %v", got)
	}
	if got := scope("overlapAudit"); got != (naReason{NACatalog, "site.scopes"}) {
		t.Errorf("a scope's catalog gap: %v", got)
	}
	if got := scopeReason(model.TestScope{})("hqCountry"); got != reasonFor("hqCountry") {
		t.Errorf("a scope without codes: %v", got)
	}
	if s := (naReason{NACatalog, ""}).sourceOrNull(); s != nil {
		t.Errorf("no source: %q, want null", *s)
	}
}

// Every gap of the production-sized build's report has a registered reason
// code and source, and no target, scope or profile is named.
func TestTheCoverageReportServesCodes(t *testing.T) {
	t.Parallel()
	cov := targetsScaleBuild(t, testscale.WeekInputs).Coverage
	codes, sources := registered(t, "na_reason"), registered(t, "na_source")
	check := func(where string, reason string, source *string) {
		r := naReason{code: reason}
		if source != nil {
			r.source = *source
			if r.source == "" {
				t.Errorf("%s: empty source, want null", where)
			}
		}
		checkReason(t, where, r, codes, sources)
	}
	gaps := 0
	entities := append(append(append([]CoverageEntity{cov.Dataset}, cov.Providers...), cov.Targets...), cov.Scopes...)
	for _, e := range append(entities, cov.Profiles...) {
		for _, fields := range e.Sections {
			for _, f := range fields {
				check(e.ID+" "+f.Path, f.Reason, f.Source)
				gaps++
			}
		}
	}
	for p, f := range cov.Fields {
		check("fields "+p, f.Reason, f.Source)
	}
	for _, list := range [][]CoverageEntity{cov.Targets, cov.Scopes, cov.Profiles} {
		for _, e := range list {
			if e.Name != "" {
				t.Errorf("%s is named %q", e.ID, e.Name)
			}
		}
	}
	if gaps == 0 || len(cov.Fields) == 0 {
		t.Fatalf("the report has %d gaps and %d fields", gaps, len(cov.Fields))
	}
}
