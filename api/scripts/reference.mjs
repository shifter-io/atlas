// Prints what the website's own TypeScript computes from fixtures/demo/design-demo.json
// and fixtures/measured/snapshot.json, so the Go port can be compared with it
// (internal/build/reference_test.go). Read-only: bundles src/lib with the
// site's esbuild into a temp file and imports it. The site now loads its data
// from this API (src/lib/site-api.ts); for the reference that loader is swapped
// for src/lib/site-api.fixture.ts, which runs presentation.ts over the fixtures,
// so the comparison is still TypeScript against Go rather than Go against itself.
//
//   node api/scripts/reference.mjs [tape-cases.json] > reference.json
//
// tape-cases.json, when given, is {events, products, generatedAt}; the output
// then carries tapeFromEvents(events, products, generatedAt) as `tape`.
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");
const { build } = await import(pathToFileURL(join(root, "node_modules", "esbuild", "lib", "main.js")).href);
const dir = mkdtempSync(join(tmpdir(), "siteapi-reference-"));
const entry = join(dir, "entry.ts");
writeFileSync(
  entry,
  `import { dataset, providers } from ${JSON.stringify(join(root, "src/lib/data.ts"))};
import { windowStats, fleetWindows, costBoards, RANGES } from ${JSON.stringify(join(root, "src/lib/window.ts"))};
import { fleetViews } from ${JSON.stringify(join(root, "src/lib/overview.ts"))};
import { tapeFromEvents } from ${JSON.stringify(join(root, "src/lib/tape.ts"))};
export { tapeFromEvents };
const ref = (p) => (p ? { slug: p.slug, name: p.name, source: p.source } : null);
const bill = (b) => (b ? { provider: ref(b.provider), bill: b.bill } : null);
export const out = {
  dataset,
  windows: Object.fromEntries(providers.map((p) => [p.slug, Object.fromEntries(RANGES.map((r) => [r.id, windowStats(p, r.id)]))])),
  fleetWindows,
  costBoards: RANGES.map((r) => ({ range: r.id, ranks: Object.fromEntries(costBoards.get(r.id).ranks), best: costBoards.get(r.id).best, dearest: costBoards.get(r.id).dearest })),
  fleetViews: fleetViews.map((v) => ({
    ...v,
    fastest: { provider: ref(v.fastest.provider), stats: v.fastest.stats },
    mostReliable: { provider: ref(v.mostReliable.provider), stats: v.mostReliable.stats },
    cheapest: { provider: ref(v.cheapest.provider), cost: v.cheapest.cost },
    dearest: { provider: ref(v.dearest.provider), cost: v.dearest.cost },
    bills: {
      all: v.bills.all.map((b) => ({ provider: ref(b.provider), bill: b.bill })),
      best: bill(v.bills.best),
      worst: bill(v.bills.worst),
      spreadX: v.bills.spreadX,
    },
  })),
};
`,
);
try {
  const outfile = join(dir, "bundle.mjs");
  const fixtureLoader = {
    name: "site-api-fixture",
    setup(b) {
      b.onResolve({ filter: /\/site-api(\.ts)?$/ }, () => ({ path: join(root, "src/lib/site-api.fixture.ts") }));
    },
  };
  await build({ entryPoints: [entry], bundle: true, format: "esm", platform: "node", outfile, logLevel: "error", plugins: [fixtureLoader] });
  const { out, tapeFromEvents } = await import(pathToFileURL(outfile).href);
  if (process.argv[2]) {
    const c = JSON.parse(readFileSync(process.argv[2], "utf8"));
    out.tape = tapeFromEvents(c.events, c.products, c.generatedAt);
  }
  process.stdout.write(JSON.stringify(out));
} finally {
  rmSync(dir, { recursive: true, force: true });
}
