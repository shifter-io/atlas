// Writes fixtures/measured/snapshot-every-request.json: the committed v1 Shifter
// fixture (fixtures/measured/snapshot.json) rewritten in the snapshot shape of
// the framework's every-request scoring (milf branch `score-from-all-requests`,
// calculated/calculator.py build(), aggregate.py timed()/with_under_load()/
// latency_detail()/pool()/pricing()). Test input only: the API's Go tests and
// the site's Vitest read it, so both sides parse the same new-shape payload.
// Deterministic; the long per-IP lists are cut so the file stays small.
//
//   node scripts/every-request-fixture.mjs
import { readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const s = JSON.parse(readFileSync(join(root, "fixtures/measured/snapshot.json"), "utf8"));
const P = ["p50", "p90", "p95", "p99"];
const scale = (lat, f) => Object.fromEntries(P.map((p) => [p, lat[p] == null ? null : Math.round(lat[p] * f)]));
// Light load is the pulse-only latency; under load is light × the load factor.
const FACTOR = { p50: 1.42, p90: 1.38, p95: 1.35, p99: 1.31 };
const under = (light) => Object.fromEntries(P.map((p) => [p, light[p] == null ? null : Math.round(light[p] * FACTOR[p] * 1000) / 1000]));
const slotsFor = { "24h": 48, "7d": 336, "30d": 1440 };

s.schema_version = 2;
s.events = [];
s.traffic = {
  period: { start: s.periods["30d"].start, end: s.as_of },
  by_test: {
    census: { runs: 4, requests: 3000, bytes_up: 300000, bytes_down: 2100000 },
    pulse: { runs: 96, requests: 1600, bytes_up: 160000, bytes_down: 1120000 },
    load: { runs: 48, requests: 400, bytes_up: 40000, bytes_down: 280000 },
  },
};
const p = s.products[0];
p.census = [
  { country: "US", region: "nyc1", target: "generic_http", finished_at: "2026-09-26T06:12:00+00:00", requests: 3000, successes: 2994, unique_ips: 2980, duration_s: 412.5, stop_reason: "unique_ratio", cities: 310, asns: 41 },
];
p.outages = [
  { country: "US", region: "nyc1", source: "pulse", started_at: "2026-09-26T08:00:00+00:00", ended_at: "2026-09-26T10:00:00+00:00", duration_s: 7200 },
];
p.pricing = {
  currency: "USD",
  lowest_per_gb: 0.75,
  entry_per_gb: 1.0,
  effective_from: "2026-09-20T00:00:00+00:00",
  valid_until: "2027-01-01T00:00:00+00:00",
  sources: [
    "https://shifter.io/services/residential-proxies observed 2026-10-03: Residential, Full Geo pool (the pool we test). Entry rate $1.00/GB: Starter 50 GB $50/mo; Basic 100 GB $100/mo.",
    "https://shifter.io/services/residential-proxies observed 2026-10-03: Residential, Full Geo pool (the pool we test). Lowest rate $0.75/GB: Growth 400 GB $299/mo; Business 200 GB $149/mo is also $0.75/GB.",
  ],
};
p.criteria = { ...p.criteria, concurrency: null, session_policy: null, schedule: "census_pulse" };
if (p.scoring) {
  delete p.scoring.authenticity;
  p.scoring.hosting_usage_types = ["hosting", "datacenter"];
  p.scoring.slots = { min_latency_successes: 30, minutes: 30 };
}

// Every latency figure is measured on one target, generic_http (framework
// da48f60): other targets keep success, blocks and the rest, with null latency
// and a latency_reason.
const LATENCY_REASON = "Latency is measured on the latency target only";
const NULL_LATENCY = { p50: null, p90: null, p95: null, p99: null };
// Pulses that overlapped a census of the same product (framework 60eabd8) are
// left out of light-load latency, time-slot success and the load factor.
const DURING_CENSUS = 6;

function everyRequest(m, slots, down, light, latencyTarget) {
  const requestRate = m.success_rate;
  return {
    ...m,
    request_success_rate: requestRate,
    success_rate: requestRate == null ? null : Math.round((requestRate - 0.0004) * 1e6) / 1e6,
    latency_ms: light ?? NULL_LATENCY,
    latency_under_load_ms: light ? under(light) : NULL_LATENCY,
    latency_reason: light ? null : LATENCY_REASON,
    under_load: light
      ? { slots: slots - down, own_factor: slots - down - 2, carried_factor: 2, without_factor: 0, reason: null }
      : { slots: 0, own_factor: 0, carried_factor: 0, without_factor: 0, reason: LATENCY_REASON },
    // Slots are lcm(pulse interval, 5 min): 30 minutes in production.
    time_slots: {
      minutes: 30, success: slots, down, latency: light ? slots - down - DURING_CENSUS : 0,
      latency_targets: light ? [latencyTarget] : [], min_latency_successes: 30, skipped_during_census: DURING_CENSUS,
    },
    coverage_slots: { due: slots, filled: slots, down },
    challenge_rate: 0.004,
    block_rate: 0.002,
    bytes_up: m.completed * 100,
    bytes_down: m.completed * 700,
    success_basis: "mean of time-slot success rates, every observed request",
    latency_basis: "light-load successes (pulses) on the latency target, each slot normalised to equal weight",
  };
}

const overlap = (runs, requests, successes, latency) => ({
  runs, requests, successes,
  request_success_rate: requests ? successes / requests : null,
  success_rate: requests ? Math.round((successes / requests - 0.001) * 1e6) / 1e6 : null,
  latency_ms: latency ?? NULL_LATENCY,
});

for (const [label, w] of Object.entries(p.windows)) {
  const slots = slotsFor[label];
  // Four slots the provider was down in (the 2-hour US outage).
  const down = 4;
  const downtime = 7200;
  const value = 1 - downtime / (w.period.hours * 3600 * 2);
  const light = scale(w.targets.generic_http.metrics.latency_ms, 0.92);
  // The v1 fixture never measured HTTPS; this one does, with 1,000 requests.
  const https = w.targets.generic_https.metrics;
  Object.assign(https, { completed: 1000, successes: 998, errors: 2, body_bytes: 14000, success_rate: 0.998, timeout_rate: 0.001, error_categories: { timeout: 1, tls: 1 } });
  for (const [id, t] of Object.entries(w.targets)) {
    t.metrics = everyRequest(t.metrics, slots, down, id === "generic_http" ? light : null, "generic_http");
    t.benchmark = t.metrics;
  }
  // The window's latency is the latency target's.
  w.metrics = { ...everyRequest(w.metrics, slots, down, light, "generic_http"), uptime: value };
  w.benchmark = w.metrics;
  w.uptime = { batches: slots, up: slots - down, value, downtime_s: downtime, basis: "outage time", countries: 2 };
  w.tests = { census: 3000, pulse: 1600, load: 400, unscheduled: 0 };
  const row = (country, region, f) => ({
    country, region,
    latency_ms: scale(light, f), latency_under_load_ms: under(scale(light, f)),
    slots: slots - down, own_factor: slots - down - 2, carried_factor: 2, without_factor: 0, reason: null,
  });
  // One reference country per runner region, picked daily as its fastest country.
  const region = (name, country, f) => ({
    region: name,
    reference: { country, target: "generic_http", concurrency: 1000 },
    chosen: [{ day: "2026-09-26", country, basis: "fastest light-load p50 of the previous day", p50_ms: Math.round(light.p50 * f), successes: 1200, candidates: 2 }],
    factors: {
      slots: slots - down - 2 - DURING_CENSUS,
      excluded_during_census: DURING_CENSUS,
      mean: FACTOR,
      latest: { at: "2026-09-26T14:00:00+00:00", ...FACTOR },
    },
  });
  w.latency = {
    target: "generic_http",
    regions: [region("fra1", "DE", 1.1), region("nyc1", "US", 1)],
    basis: "under load = light-load (pulse) latency x (load run ÷ pulse in the region's reference country) of the same slot, else the region's newest earlier factor; mean of slot and country values",
    countries: [row("DE", "fra1", 1.1), row("US", "nyc1", 1)],
  };
  const pulseRuns = 96;
  w.census_overlap = {
    pulses: { clean: overlap(pulseRuns - DURING_CENSUS * 2, 9000, 8990, light), during_census: overlap(DURING_CENSUS * 2, 1200, 1110, scale(light, 1.3)) },
    targets: {
      generic_http: { clean: overlap(pulseRuns / 2 - DURING_CENSUS, 4500, 4496, light), during_census: overlap(DURING_CENSUS, 600, 556, scale(light, 1.3)) },
      generic_https: { clean: overlap(pulseRuns / 2 - DURING_CENSUS, 4500, 4494, null), during_census: overlap(DURING_CENSUS, 600, 554, null) },
    },
    load: { clean: slots - DURING_CENSUS, during_census: DURING_CENSUS },
    census_batches: [101, 102],
    basis: "pulse and load runs that overlapped a running census of the same product (any country), from stored run start and finish times; left out of light-load latency, time-slot success and the load factor, still counted as requests, uptime and slots",
  };
  // Per unique exit IP; IPs without the DB-IP field are left out of each share.
  w.pool.geo_accuracy = 0.987;
  w.pool.datacenter_asn_share = 0.004;
  w.pool.classification_coverage = 0.998;
  w.pool.shares_basis = "unique exit IPs with the DB-IP field (unknowns excluded)";
  w.pool.geo = w.pool.geo.slice(0, 12);
  w.pool.asns_breakdown = w.pool.asns_breakdown.slice(0, 12);
  w.ip_samples = [];
  w.breakdowns = {
    country: [
      { country_code: "US", country: "United States", ips: 4000, observations: 4100, completed: 4200, successes: 4195, challenges: 10, blocks: 5, success_rate: 0.9988, p50: light.p50, p90: light.p90, p99: light.p99 },
      { country_code: "DE", country: "Germany", ips: 900, observations: 900, completed: 800, successes: 798, challenges: 2, blocks: 1, success_rate: 0.9975, p50: Math.round(light.p50 * 1.1), p90: Math.round(light.p90 * 1.1), p99: Math.round(light.p99 * 1.1) },
    ],
  };
  for (const score of Object.values(w.scores)) {
    // Alone in its kind (as on staging): every ready part scores 100 against itself.
    Object.assign(score, { score: 100, grade: "A+", state: "ready", rank: null, reasons: [] });
    for (const comp of Object.values(score.components)) {
      Object.assign(comp, { value: 100, state: "ready", reason: null });
      comp.inputs = comp.inputs && typeof comp.inputs === "object" ? { ...comp.inputs, peers: 1 } : { value: null, best: null, peers: 1 };
    }
    const c = score.components;
    if (c.authenticity) c.authenticity = { value: 100, state: "ready", reason: null, inputs: { value: 98.7, best: 98.7, peers: 1 } };
    if (c.value) c.value = { value: 100, state: "ready", reason: null, inputs: { value: 0.75, best: 0.75, peers: 1 } };
    if (c.speed) c.speed = { ...c.speed, inputs: { peers: 1, targets: { generic_http: { value: w.targets.generic_http.metrics.latency_under_load_ms.p50, best: w.targets.generic_http.metrics.latency_under_load_ms.p50 } } } };
  }
  // Other targets score reliability only: speed is null with weight 0.
  const ts = w.targets.generic_https.score;
  if (ts?.components?.speed) {
    ts.components.speed = { value: null, state: "unavailable", reason: LATENCY_REASON, inputs: { value: null, best: null, peers: 0 } };
    if (ts.weights) ts.weights.speed = 0;
  }
}

writeFileSync(join(root, "fixtures/measured/snapshot-every-request.json"), JSON.stringify(s, null, 1) + "\n");
