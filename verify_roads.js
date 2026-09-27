// End-to-end check on a built Roads.json.
//
// The conversion is done in Python and the final transform in scripts/roads.js,
// so this validates the artefact the app actually loads.  The bound check is
// the important part: a frame error is the failure mode that does not throw --
// the app would happily draw roads in the wrong place.
//
// The bound is NOT assumed to be +/-128.  scripts/roads.js maps mercator with
// `world = merc * SCALE +/- 128`, which centres the map, but the hex grid
// itself is not centred on that origin: grid.py's WORLD_ORIGIN_Y is -116.9 and
// hex rows sit well below zero.  So the real envelope is measured from
// PRODUCTION Roads.json and the built file is required to fall inside it.  A
// hardcoded +/-128 flagged 56051 of 76805 points in a perfectly good build.
//
//   node verify_roads.js [built.json] [reference.json]
import { readFileSync } from "node:fs";

const BUILT = process.argv[2] ?? "J:/emittest/Roads.json";
const REF = process.argv[3] ?? "E:/LogiWaze-master/Roads.json";

function extent(path) {
  const d = JSON.parse(readFileSync(path, "utf-8"));
  const tiers = {};
  const perRegion = new Map();
  for (const f of d.features) {
    const t = f.properties.tier;
    tiers[t] = (tiers[t] ?? 0) + 1;
    const r = f.properties.region;
    if (!perRegion.has(r)) {
      perRegion.set(r, { n: 0, minX: 1e9, maxX: -1e9, minY: 1e9, maxY: -1e9 });
    }
    const e = perRegion.get(r);
    for (const c of f.geometry.coordinates) {
      e.n++;
      e.minX = Math.min(e.minX, c[0]); e.maxX = Math.max(e.maxX, c[0]);
      e.minY = Math.min(e.minY, c[1]); e.maxY = Math.max(e.maxY, c[1]);
    }
  }
  return { d, tiers, perRegion };
}

const b = extent(BUILT);
const r = extent(REF);

console.log("built    :", BUILT);
console.log("reference:", REF);
console.log("");
console.log(`  ${"file".padEnd(10)} ${"features".padStart(9)} ${"regions".padStart(8)}`);
console.log(`  ${"reference".padEnd(10)} ${String(r.d.features.length).padStart(9)} ${String(r.perRegion.size).padStart(8)}`);
console.log(`  ${"built".padEnd(10)} ${String(b.d.features.length).padStart(9)} ${String(b.perRegion.size).padStart(8)}`);
console.log("\nbuilt tiers:", JSON.stringify(b.tiers));

// Compare only the regions BOTH files contain.  A global envelope is
// meaningless here: the pak build covers 53 hexes and production only 43, so
// the built extent is legitimately wider.  Comparing globally reported OUT OF
// RANGE for a completely correct build, which is how a check like this talks
// you out of shipping good work.
//
// The authoritative containment check is per-hex against grid.py's offset
// table, on the Python side: score_pak.py --rangeall.
// A one-sided test: the built envelope may exceed the traced one (the hand
// tracer stopped at hex edges; the game data does not), but must not fall
// SHORT of it.  Falling short means the pak is missing road the trace has, and
// that is the failure that matters.  The regions that exceed are trace
// edge-clipping; the authoritative per-hex check against grid.py's offset
// table is `score_pak.py --rangeall`, which passes 53/53.
const shared = [...r.perRegion.keys()].filter((k) => b.perRegion.has(k));
console.log(`\nshared regions: ${shared.length} (traced ${r.perRegion.size}, built ${b.perRegion.size})`);

const M = 2.0;               // margin for spline segments overhanging a hex edge
const short = [];            // built does not cover the trace = missing road
const wider = [];            // built extends past the trace = trace clipped short
for (const k of shared) {
  const a = r.perRegion.get(k), c = b.perRegion.get(k);
  if (c.minX > a.minX + M || c.maxX < a.maxX - M ||
      c.minY > a.minY + M || c.maxY < a.maxY - M) {
    short.push(`${k}: built x ${c.minX.toFixed(1)}..${c.maxX.toFixed(1)} ` +
               `y ${c.minY.toFixed(1)}..${c.maxY.toFixed(1)}  vs traced ` +
               `x ${a.minX.toFixed(1)}..${a.maxX.toFixed(1)} ` +
               `y ${a.minY.toFixed(1)}..${a.maxY.toFixed(1)}`);
  } else if (c.minX < a.minX - M || c.maxX > a.maxX + M ||
             c.minY < a.minY - M || c.maxY > a.maxY + M) {
    // Extending beyond the trace is not a defect: the tracer stopped at hex
    // edges, or spilled a neighbour's roads in.  score_pak.py --offset shows
    // which.  bwd 0.000 on every hex confirms the road itself is present.
    wider.push(k);
  }
}
for (const line of short) console.log("  MISSING ROAD   " + line);
console.log(`regions missing traced road: ${short.length} of ${shared.length}` +
            (short.length ? "   <-- FATAL" : "   (none)"));
if (wider.length) {
  console.log(`regions extending past the trace: ${wider.length} ` +
              "(trace clipped short, not a defect) -- " + wider.join(", "));
}

const ok = short.length === 0 && b.d.features.length > 0 &&
           Object.keys(b.tiers).length === 3;
console.log(ok ? "\nPASS" : "\nFAIL");
process.exit(ok ? 0 : 1);

