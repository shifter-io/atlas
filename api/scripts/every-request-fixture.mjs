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

// HTTPS pays for CONNECT + TLS, so its light-load latency is about twice HTTP's.
const HTTPS = 2.1;
const mix = (a, b) => Object.fromEntries(P.map((p) => [p, a[p] == null || b[p] == null ? a[p] ?? b[p] : Math.round((a[p] + b[p]) / 2)]));
const targetsOf = { generic_http: 1, generic_https: HTTPS };

function everyRequest(m, slots, down, light, targets) {
  const requestRate = m.success_rate;
  return {
    ...m,
    request_success_rate: requestRate,
    success_rate: requestRate == null ? null : Math.round((requestRate - 0.0004) * 1e6) / 1e6,
    latency_ms: light,
    latency_under_load_ms: under(light),
    under_load: { slots: (slots - down) * targets.length, own_factor: (slots - down - 2) * targets.length, carried_factor: 2 * targets.length, without_factor: 0, reason: null },
    // Slots are lcm(pulse interval, 5 min): 30 minutes in production.
    time_slots: { minutes: 30, success: slots, down, latency: slots - down, latency_targets: targets, min_latency_successes: 30 },
    coverage_slots: { due: slots, filled: slots, down },
    challenge_rate: 0.004,
    block_rate: 0.002,
    bytes_up: m.completed * 100,
    bytes_down: m.completed * 700,
    success_basis: "mean of time-slot success rates, every observed request",
    latency_basis: "light-load successes (pulses), each slot and target normalised to equal weight",
  };
}

for (const [label, w] of Object.entries(p.windows)) {
  const slots = slotsFor[label];
  // Four slots the provider was down in (the 2-hour US outage).
  const down = 4;
  const downtime = 7200;
  const value = 1 - downtime / (w.period.hours * 3600 * 2);
  const http = scale(w.targets.generic_http.metrics.latency_ms, 0.92);
  const lightOf = { generic_http: http, generic_https: scale(http, HTTPS) };
  // The v1 fixture never measured HTTPS; this one does, with 1,000 requests.
  const https = w.targets.generic_https.metrics;
  Object.assign(https, { completed: 1000, successes: 998, errors: 2, body_bytes: 14000, success_rate: 0.998, timeout_rate: 0.001, error_categories: { timeout: 1, tls: 1 } });
  for (const [id, t] of Object.entries(w.targets)) {
    t.metrics = everyRequest(t.metrics, slots, down, lightOf[id], [id]);
    t.benchmark = t.metrics;
  }
  // The window mixes both targets at equal weight: never "the" latency.
  w.metrics = { ...everyRequest(w.metrics, slots, down, mix(lightOf.generic_http, lightOf.generic_https), Object.keys(targetsOf)), uptime: value };
  w.benchmark = w.metrics;
  w.uptime = { batches: slots, up: slots - down, value, downtime_s: downtime, basis: "outage time", countries: 2 };
  w.tests = { census: 3000, pulse: 1600, load: 400, unscheduled: 0 };
  const light = lightOf.generic_http;
  const row = (country, target, f) => ({
    country, target,
    latency_ms: scale(lightOf[target], f), latency_under_load_ms: under(scale(lightOf[target], f)),
    slots: slots - down, own_factor: slots - down - 2, carried_factor: 2, without_factor: 0, reason: null,
  });
  w.latency = {
    // The census target's load wave, started 60 s after the slot's pulses.
    reference: { country: "US", target: "generic_http", concurrency: 1000 },
    factors: {
      slots: slots - down - 2,
      mean: FACTOR,
      latest: { at: "2026-09-26T14:00:00+00:00", ...FACTOR },
    },
    basis: "under load = light-load (pulse) latency x (load run ÷ reference-country pulse) of the same slot, else the newest earlier slot's factor; mean of slot values; per country and target",
    countries: [row("DE", "generic_http", 1.1), row("DE", "generic_https", 1.1), row("US", "generic_http", 1), row("US", "generic_https", 1)],
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
    const c = score.components;
    if (c.authenticity) c.authenticity = { value: 100, state: "ready", reason: null, inputs: { value: 98.7, best: 98.7, peers: 1 } };
    if (c.value) c.value = { value: 100, state: "ready", reason: null, inputs: { value: 0.75, best: 0.75, peers: 1 } };
    if (c.speed) c.speed = { ...c.speed, inputs: { peers: 1, targets: Object.fromEntries(Object.entries(w.targets).map(([id, t]) => [id, { value: t.metrics.latency_under_load_ms.p50, best: t.metrics.latency_under_load_ms.p50 }])) } };
  }
}

writeFileSync(join(root, "fixtures/measured/snapshot-every-request.json"), JSON.stringify(s, null, 1) + "\n");
