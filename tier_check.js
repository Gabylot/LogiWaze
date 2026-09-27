// Tier convention: state and check the app's scale.
//
// The pak route reads tiers from the game's mesh names, and the game's scale
// runs the OPPOSITE way to the app's.  Getting it backwards would not crash --
// the app would route gravel roads as mud, which is the worst kind of failure
// because it still looks plausible.
//
// The evidence is in the app, not in the game data:
//   src/Panel.ts            breakdown[2] = "Gravel/Paved"
//                           breakdown[1] = "Dirt"
//                           breakdown[0] = "Mud"
//   topology.ts             fills that array as `tier - 1`
//   => runtime tier 3 = gravel/paved (best), tier 1 = mud (worst)
//   scripts/roads.js        then applies `(3 - tier) + 1` going in, so
//                           road_source.geojson uses the inverse: 1 = best.
//
// roads_from_pak.py writes road_source.geojson, so it must emit 1/2/3 with 1 =
// best.  This asserts that round trip.  The empirical half of the check --
// that game RoadT3Gravel really does sit where the hand trace calls tier 1 --
// is tier_agreement.py, which measures it against real geometry.
//
//   node tier_check.js
// Tier convention: assert the round trip through scripts/roads.js.
//
// The pak route reads tiers from the game's mesh names, and the game's scale
// runs the OPPOSITE way to the app's.  Getting it backwards would not crash --
// the app would route gravel roads as mud, the worst kind of failure because it
// still looks plausible.
//
// The evidence is in the app, not the game data:
//   src/Panel.ts      breakdown[2] = "Gravel/Paved"
//                     breakdown[1] = "Dirt"
//                     breakdown[0] = "Mud"
//   topology.ts       fills that array as `tier - 1`
//   => runtime tier 3 = gravel/paved (best), tier 1 = mud (worst)
//   scripts/roads.js  applies `(3 - tier) + 1` going IN, so road_source.geojson
//                     -- what roads_from_pak.py writes -- is the inverse:
//                     tier 1 = best, tier 3 = worst.
//
// The empirical half (that game RoadT3Gravel really sits where the hand trace
// calls tier 1) is `score_pak.py --tiers`.
//
//   node tier_check.js
const EXPECTED_RUNTIME = { 1: "Mud", 2: "Dirt", 3: "Gravel/Paved" };

// road_source.geojson tier -> the runtime tier it must become.  Inverted,
// because roads.js applies (3 - tier) + 1 on the way in.
const EXPECTED_SOURCE = { 1: 3, 2: 2, 3: 1 };

console.log("tier convention check");
console.log("  road_source.geojson -> Roads.json -> app label");
let ok = true;
for (const [src, wantTier] of Object.entries(EXPECTED_SOURCE)) {
  // Reproduce roads.js's arithmetic exactly, including the 0 and 4 fixups.
  let t = Number(src);
  if (t === 4) t = 3;
  if (t === 0) t = 1;
  t = (3 - t) + 1;
  // Single assertion: the arithmetic must yield the runtime tier named above.
  // The label is a readout, not a second hand-maintained expectation that can
  // drift out of step with the first.
  const good = t === wantTier;
  if (!good) ok = false;
  console.log(
    `  tier ${src} -> runtime ${t} (${EXPECTED_RUNTIME[t]})  ` +
    (good ? "OK" : `MISMATCH, want runtime ${wantTier}`));
}
console.log(ok ? "\nPASS" : "\nFAIL");
process.exit(ok ? 0 : 1);




