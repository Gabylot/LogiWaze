# Road extraction — state of work

Handover notes. Read this before touching the extractor; most of it records
conclusions that cost real time to reach and will otherwise be re-litigated.

## What production actually is

    road_source.geojson  --scripts/roads.js-->  Roads.json  -->  app
    (QGIS-traced, EPSG:3857)   mercator->world     (the app's data)

`index.html` embeds road_source/Roads data directly. The app is built on
**leaflet-routing-machine**; turn instructions are derived from polyline
geometry, so polyline shape is player-visible.

**The app has no terrain logic at all** — zero `driveable`, `landmass`, or
water rules. It routes purely on the road graph.

`road_overlays/` is a diagnostic artefact and is referenced by no app code.

## Geometry reference (authoritative)

`MapStitcher/map.xml` is the canonical hex layout: a 10x7 grid, each hex at
`(x, y)` in half-steps of the hex size. It agrees **53/53** with the
`offsetx`/`offsety` table in `scripts/export_major_locations.sh`, which is what
`grid.py` parses. `grid.canonical_region()` normalises the two spellings
(`MapStemaLAndingHex.png` vs `StemaLandingHex`).

`index.html` stores hexes as a **per-column shear** (`y = slope*x`, slope in
{0, +-0.5, +-1}) — not an absolute position. Do not compare it to an absolute
y; doing so produced a phantom "y-frame disagreement" that does not exist.

## Two bugs found and fixed

1. **9.5px origin error** in `grid.py` (`WORLD_ORIGIN` 115.0985,-116.9410 ->
   115.21725,-116.90975). The constants had been fitted against Roads.json, so
   they absorbed the error instead of exposing it. Effect: F1 55.1 -> 86.5,
   recall 58% -> 97.3%.
2. **Tier-2 saturation cut** (`TIER2_MIN_SAT = 88`). Dark dull terracotta
   (181,117,97, sat 84) shades terrain and was being traced as road. Real
   tier-2 roads are sat 102-109. False ink fell 8-12x with **no** recall cost.

## Measured accuracy (vs hand-drawn Roads.json, 43 hexes)

| measure | result |
|---|---|
| geometric agreement, median | 86.8% within 4px |
| geometric agreement | 96.2% within 8px |
| turn-instruction agreement | 78.3% (122.9 turns vs 95.0 hand) |
| real town pairs routable | 175/200 |
| roads crossing water | 0.000% |
| hexes building cleanly | 50/55 |

Residual distance distribution: p50 3.0px, p95 7.6px, then a cliff to p99
36.8px. The sub-8px bulk is lateral offset between the traced line and the
painted centreline (irreducible); the ~2-3% beyond 20px is genuine missing road.

## Levers already ruled out — do not re-try

| lever | measured result |
|---|---|
| tier-1 radius, thickness, component size | **zero effect** (identical at 4/6/8) |
| walk length filters, stub filters | +0.6 points for +24% features |
| a fourth colour tier | tested, **reverted** — recall up, precision down |
| `eps` simplification | turn match *worse*; 0.06 is correct |
| missing colour classes | **do not exist** — missed colours are ones we already admit |

The one live defect: **~30% excess turns** (122.9 vs 95.0). Hand-traced lines
are simplified once by a human; pixel extraction follows the skeleton
literally. `eps` cannot fix it — simplification overshoots to a turn deficit.

## Tooling (all in /var/www/LogiWaze)

    build_roads.py       the one command: screen -> emit -> verify -> merge
    check_new_hex.py     screen for uncharted hexes; catches breakage
    emit_road_source.py  writes production road_source.geojson format
    netdiff.py           bidirectional chamfer metric (the accurate one)
    turn_check.py        turn instructions on the POLYLINES the app routes on
    route_check.py       path/turn/distance comparison vs hand-drawn
    validate_all.py      route cross-check (weak; see ROUTE_METRIC.md)
    extract_routes.py    core extractor
    grid.py              origin table + hex grid
    routegraph.py        contracted graph
    test_roads.py        regression tests

One command builds everything:

    python3 build_roads.py --all --out new_roads.geojson

Output is verified through the real `scripts/roads.js` (p50 0.33px on the map).

## Metrics — which to trust

**Trust `netdiff.py`.** It is bidirectional (forward = invented roads,
backward = missed roads) and its sensitivity is proven: a 15px shift collapses
it (66.5% -> 22.9%), and erasing 40% of the network drops backward 73% -> 43%.

**Distrust route-length comparison.** Correlation with actual overlap is
**0.158** — essentially blind. Three hexes scored a perfect 0.999 ratio while
overlapping only 50-58%. Two networks can share no road and have identical
total length.

**Turn agreement must be measured on the POLYLINES**, not the skeleton
graph — routing on the skeleton gave 34.7% where the real figure is 78.3%.

## Open items

- `all_roads.geojson` (50 hexes, 14,871 features) is built and verified in
  /tmp but **NOT deployed**. Swapping it for 20,972 hand-traced features is a
  human decision; the turn excess is a real player-facing regression.
- Remaining ~2-3% genuine missed road: parallel roads absorbed by medial-axis
  skeletonisation. Needs a different algorithm, not tuning.
- **Next real step: the pak file.** `scripts/extract_roads.js` already expects
  `AcrithiaHex.json` containing mesh objects named `T1Road`/`T2Road`/`T3Road`
  with `Properties.Points` and `PhysMaterial.ObjectName`. That file does not
  exist in the repo — someone tried this route before. Game mesh data gives
  exact geometry and true road tiers, which would beat pixel extraction
  outright and eliminate the turn-excess problem.
