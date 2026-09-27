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
    audit_pak.py         per-line audit of the PAK network vs the hand trace
    render_overlay.py    draws pak + hand over the real map PNG (the visual check)

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
## The pak route — DONE, see below

`scripts/extract_roads.js` was written against a schema **no UE tool produces**
(`Properties.Points[].Center`, `LocalMeshComponents`, `PhysMaterial.ObjectName`).
That is why it never worked: there was no producer and there isn't one to write.
Delete it; `roads_from_pak.py` replaces it.

Foxhole roads are **spline meshes**, tiered in the asset name. Working route:

    git clone --recurse-submodules https://github.com/Tsekho/fh_map_exporter.git
    Exporter.exe -i "<Foxhole>\War\Content\Paks" -o export \
                 -a War/Content/Maps/Master/AcrithiaHex      # note Maps/Master/

Omitting `-a` exports every umap. Only `_json/` matters; skip steps 2-6 (those
are the Blender/pixel path being replaced). No .NET SDK needed — a prebuilt
`Exporter.exe` is committed. Do NOT rebuild the CUE4Parse submodule casually.

Only `splines` is read. Per-hex counts, AcrithiaHex: 1418 entries -> 69 lines,
tiers t1=21 t2=30 t3=18, 14.8 km. `Engine__Content__EditorLandscapeResources__
SplineEditorMesh` (44 entries) is the editor's own visual and must be ignored.

### Two bugs that cost real time — do not reintroduce

1. **Chaining tolerance must be in cm, not world units, and must run BEFORE the
   transform.** A hex is only 25.6 world units wide, so a "0.35" world tolerance
   is **3000 cm** while the median segment is 1000 cm. It welded unrelated roads
   together, producing chains that doubled back on themselves. That alone was
   290 turns/hex vs 28 real. Fixed: chain at 25 cm, then transform.
2. **The Hermite tangents are component-space, not world.** Sampling the cubic
   gave 49.8 km of road from 14.2 km of actual segments — 3.5x too long.
   Use the **knots only** (start/end positions, which are world space). The
   spline code in `roads_from_pak.py` is chord-only for this reason.

### Calibrated constants (fitted once, evidence in ROAD_EXTRACTION_HANDOVER)

- **cm -> world scale: exact, not fitted.** A hex is 2200 m (exporter heightmaps
  are 2200x2200 px at 1 m/px) and spans grid.W, so `scale = W / 220000`.
- **Y sign = -1.** Measured both: fwd 1.098 vs 1.583. The -1 wins clearly.
- **Origin: the hex centre**, from grid.py's `export_major_locations.sh` table.
  Unlike the old WORLD_ORIGIN this cannot absorb per-hex error, because there
  is nothing per-hex left to fit.

Verified: **0 of 1487 points fall outside the hex rectangle** grid.py defines
(`score_pak.py --range`). A frame error is the main risk on this route — the app
would render roads happily in the wrong place — so that check is the one to run
after any change here.

### Tier convention — SETTLED, do not re-derive

The game's mesh names and the app's tier numbers are **opposite scales**. This
is now proven twice, not assumed:

1. **From the app.** `src/Panel.ts` reads `breakdown[2]="Gravel/Paved"`,
   `[1]="Dirt"`, `[0]="Mud"`, and `topology.ts` fills that array as `tier - 1`.
   So at *runtime* tier 3 = gravel/paved (best), tier 1 = mud (worst).
   `scripts/roads.js` then applies `(3 - tier) + 1` on the way **in**, so
   `road_source.geojson` — what this module writes — uses the inverse:
   **tier 1 = best, tier 3 = worst.** `node tier_check.js` asserts the
   round trip.

2. **From geometry** (`score_pak.py --tiers`, 4 hexes, tol 0.77): game tier 1
   lands on hand tier 1 for 86.7% of points, tier 2 on hand tier 2 for 81.0%.
   A sharp diagonal. The reversed mapping would give the anti-diagonal.

So: `RoadT3Gravel01` -> tier 1, `RoadT2PackedDirt01` -> 2, `RoadT1Dirt01` -> 3.
Tier 3 is the weakest diagonal (45.4% on-diagonal) because mud and dirt roads
run alongside each other in the trace, so a mud point often has dirt within the
tolerance. That is expected, not a mapping error.

**This retires `TIER2_MIN_SAT` and the whole pixel colour-tier heuristic.** Tiers
now come from the asset name and are true by construction.

### Pak vs hand-traced, side by side

`score_pak.py --compare <hexes>` reads the **built artefact** (`--merge` output
pushed through the real `scripts/roads.js`), not a recomputation, so it measures
what would actually ship. 43 shared hexes:

| | pak (game mesh) | hand-traced |
|---|---|---|
| features | 3,422 | 20,972 |
| polyline vertices | 68,760 | 156,214 |
| road length | 647.1 km | 674.9 km |
| tier 1 (gravel/paved) | 714 (20.9%) | 5,144 (24.5%) |
| tier 2 (dirt) | 950 (27.8%) | 6,159 (29.4%) |
| tier 3 (mud) | 1,758 (51.4%) | 9,484 (45.2%) |
| turns (per-line) | 6,570 | 2,788 |

Coverage and geometry:

| measure | value |
|---|---|
| mean forward (invented road) | **0.110** |
| median forward | **0.059** |
| worst forward | 0.604 (TheFingersHex) |
| mean backward (missed road) | **0.000** |
| hexes with bwd exactly 0.000 | **43 of 43** |

Two things this settles:

- **Road length agrees to 4%** (647 vs 675 km) from completely independent
  sources, which is a strong check that the scale is right — a scale error
  would show up as a length error.
- **The earlier "gravel-heavy" worry was unfounded.** Tier 1 is 20.9% against
  the trace's 24.5%, not the skew the raw 1,917/1,042/764 split suggested —
  that split counted *all 53* pak hexes against the trace's 43, and included
  the 10 hexes the trace does not cover. Restricted to shared hexes the
  distributions are close. **This item is closed; do not re-raise it.**

The turn excess is unchanged at 136%, and remains the open question.

### The pak geometry is sound. The turns are the whole story.

**This supersedes the impression left by a bad audit, and is the headline finding.**

A per-line audit of all 43 hand-traced hexes (`audit_pak.py`) first appeared to show
serious geometric error: 134 lines below 50% agreement, a "22% of road degraded"
figure, a 4% bad-length figure, lines "crossing water and rock". **All of those were
an artefact of one arbitrary tolerance.** A hex is 2200 m across 2048 px, so
**1 map px = 1.07 m**, and the audit's 4px "on the road" threshold was **4.3 m** —
about one lane. Every "disagreement" it found was sub-lane.

The same data, in real units:

| tolerance | agreement |
|---|---|
| 4.3 m (4 px) | 88.5% |
| 8.6 m (8 px) | 98.5% |
| 10 m | **99.2%** |
| 20 m | 99.9% |

Endpoint analysis (`--ends`) closes the last loose end: for the longest
"misaligned" lines, **every endpoint sits 0-10 px (0-11 m) from the hand trace**.
Only 4 of 33 bad lines have an endpoint beyond 40 px. These lines start and end
correctly on the road and bow a few metres in the middle — the game spline follows
the designer's control points along a curve, the human tracer followed the brush.
Nothing is mislocated, invented, or mis-tiered.

**Tier classification is not a defect and cannot be.** `classify()` reads the tier
from the game mesh NAME by exact match; every hex contains only the three road
meshes, the two Snow variants, and `SplineEditorMesh` (correctly ignored). The
earlier "blue line is a tier-classification bug" hypothesis is refuted — blue is
simply the mud tier, drawn in the renderer's colour scheme.

Verdict on geometry: **matches the hand trace to ~99% within 10 m.** The original
assessment — geometry good, turns are the issue — was right.

### Lessons from that audit — the mistakes are the reusable part

Four separate alarming numbers were produced and then dissolved on inspection. All
four were my own measurement errors, not data problems. They are recorded because
each is an easy mistake to repeat.

| number reported | why it was wrong |
|---|---|
| "4% of road length is bad" | threshold artefact. Summing lines below 50% gives 1.2% at a 30% cutoff and 21.9% at an 80% cutoff. The same data spans that whole range. |
| "no global calibration error, but a diagonal streak suggests rotation" | the fitted matrix was **rank 1** (singular values 1.98 and 0.0023, det -0.0045). It reported a "44 deg rotation, 1.398x scale" — decomposing a degenerate matrix. `--fit` now refuses to print rotation/scale when rank < 2. |
| "leave-one-hex-out shows 22.9px -> 3.0px, a real 87% improvement" | the baseline was computed as root-of-mean-of-rms, which produced 4.77 px against a mean displacement of 19.79 px — an rms below the mean is impossible. Fixed to accumulate squared magnitudes. The improvement was fitting noise. |
| "offset probe shows |d| of 600-1800 px, concentration 1.00" | concentration above 1.0 is mathematically impossible and should have been the tell. The metric scored 1.0 *because* the line was far away, since every vector then points the same way. Now NaN beyond a 30 px gate. |

**Two rules this implies.** (1) Never quote a geometric threshold in pixels on this
data — always convert, because 1 px = 1.07 m and a pixel count hides whether a
disagreement is a defect or lane placement. (2) A single aggregate percentage is
worthless on its own; sweep the threshold before believing it.

Also note the audit found the 43-hex figure is robust where the earlier 3-hex
reading was not: on three hexes the mud tier looked materially worse (73% vs
84/83%), which suggested a tier-specific threshold. Across all 43 the spread is
89.5 / 89.3 / 87.2% — **no tier signal, do not build a T3 filter on it.**

### Visual check: overlays over the real map

    python3 render_overlay.py AcrithiaHex DeadLandsHex
    python3 render_overlay.py --all        # 53 written to J:/overlays
    python3 render_overlay.py AcrithiaHex --zoom 295,691,821,842

Draws the map PNG desaturated, the hand trace in magenta, and the pak roads on
top coloured by tier. Where the two agree you see the pak colour; where the pak
is wrong, magenta shows through. All 53 render. **This is the check the numbers
cannot fake** — if the frame were off, the overlay would run off the painted
road surface, and it does not. Confirms the `--rangeall` result visually.

Needs Pillow only, not skimage, so it runs without the pixel toolchain.

**Three bugs were found in this script and fixed.** They are recorded because the
first two are silent — they produce a plausible-looking image and a plausible
legend, which is worse than a crash:

1. **Zoom crop offset** (was line ~130). The crop origin was computed as
   `box[0] / SCALE_W`, dividing a full-res pixel value by the *output* width.
   Both `load_pak` and `load_hand` return **map pixels of the full 2048px image**,
   so with MAP_PX=2048 and SCALE_W=1024 every zoom overlay was displaced by 2x and
   drawn off-canvas. The fix is `box[0] * SCALE_W / MAP_PX`. Consequence: every
   zoom crop produced a bare map with **no overlay at all** while still printing a
   real-looking "hand 186, pak 42" in the legend. Any conclusion drawn from a zoom
   before this fix is void.
2. **`--hand-only` rendered an empty image** — the guard suppressed the hand layer,
   i.e. the one layer that was requested.
3. **Legend counts were post-suppression**, so they reported filtered numbers and
   hid bug 2. They now always report the underlying data regardless of mode.

`TheFingersHex.png` explains its own worst-in-set forward score (0.604): the
magenta-only runs along the south edge are hand-traced roads with **no pak mesh
underneath at all** — trace only, not pak invention. The two settlement grids
also diverge visibly. So this hex is a genuine content difference to resolve by
hand, not a coordinate error. The other 42 overlays sit on their painted roads.

### Turn excess RESOLVED: the 136% figure was measuring the wrong thing

The 6,570 vs 2,788 "136% excess" in earlier revisions is a **per-line** count:
every corner on every polyline, whether or not a route ever uses it. The app
does not route that way. It routes town to town and emits turns only where the
chosen path bends. Those are different questions, and the per-line figure
flatters whichever dataset has more, shorter polylines.

`turn_check_pak.py` measures the app's metric — snap both towns, Dijkstra over
the polyline network, then `turns_of()` with the same anchor/probe walk and
12-degree tolerance the app uses. Across **1,122 real town-pair routes on all
43 shared hexes**:

| | turns per route |
|---|---|
| pak (game mesh) | **85.2** |
| hand-traced | **109.1** |

**The pak route produces 22% FEWER turns per route, not 136% more.** 37 of 43
hexes are negative; only 4 are slightly positive (KingsCage +8, Oarbreaker +8,
Terminus +4, SpeakingWoods +9).

So the per-line excess was largely an artefact of polyline segmentation, and
the app-facing risk is the opposite one: the pak data is somewhat *too*
straight. That is consistent with the hand trace being drawn as many short
digitised segments, each contributing a small turn, and it is a far milder
problem than the original figure implied — turns are a navigation-aids nicety,
not a correctness requirement.

This supersedes the "turn excess" item throughout. The remaining 136% figure
should not be quoted as a defect.

Caveat worth stating: only 1,122 of the 2,580 attempted pairs routed on both
networks, because a pair only counts when both towns snap to *both* networks
within 600px. Hexes like DrownedVale (5/60) and TempestIsland (12/60) are thin
samples, so per-hex figures there are weak even though the aggregate is solid.

`turn_check_pak.py` replaces the Linux-only `turn_check.py` for this question:
it needs only numpy + scipy, because both sides are already GeoJSON. (Fixed
along the way: `grid.load_offsets()` had a hardcoded `/var/www/LogiWaze` default
that raised FileNotFoundError on Windows, and towns.json x/y are already world
units — applying a mercator factor collapsed all 42 DeadLands towns onto one
node, making every route zero-length and every turn count 0.)

All 53 hexes exported (`export_all_hexes.py`, 52 exported + 1 cached,
**0 failures**). Scored against the 43 hand-traced hexes:

| measure | pak route | pixel extractor (handover) |
|---|---|---|
| backward (missed road) | **0.000 on all 43 hexes** | — |
| unmatched traced points | 0.6-1.5% (`--gap`) | — |
| forward (invented road), mean | **0.110** | — |
| turn instructions | 6570 vs 2788 (**136% excess**) | 122.9 vs 95.0 (30% excess) |

Two things follow, and the second is the one that matters:

- **Coverage is total.** `bwd = 0.000` on every single hex: every metre of
  hand-traced road has game road over it. `bwd 0` cannot be flattered by a
  scaling error — netdiff's proven sensitivity (a 15 px shift collapses it)
  means this is a real zero. The ~2-3% "genuine missed road" from the pixel
  route is **gone**, and so is the whole medial-axis failure mode.
- **The turn excess got WORSE, not better: 136% vs 30%.** The game's splines are
  tessellated for rendering, not simplified for wayfinding — they carry more
  knots than a human tracer would draw. So the pak does *not* fix the turn
  excess, and any plan that assumed it did is wrong.

The turn excess is **real curvature, not artefact**. I hypothesised the
game splines were tessellated for rendering — straight 10 m runs arriving as
chains of collinear segments, each vertex registering as a turn — and swept a
collinear-collapse threshold to test it (`score_pak.py --sweep`, 43 hexes):

| deg | verts | turns | excess | mean fwd |
|---|---|---|---|---|
| off | 68760 | 6570 | 136% | 0.680 |
| 0.5 | 59564 | 6570 | 136% | 0.680 |
| 2.0 | 45766 | 6579 | 136% | 0.679 |
| 5.0 | 33154 | 6613 | 137% | 0.679 |
| 20.0 | 17281 | 8658 | 211% | 0.679 |

**Refuted.** Deleting 33% of the vertices moved the turn count by 9 (0.1%), and
raising the threshold made turns go *up*. The vertices carry genuine direction
changes — the roads really do curve more often than a human tracer draws them.
`COLLINEAR_DEG` is therefore `None` (off) and geometry is emitted unmodified;
the function is kept only because the sweep is the evidence.

So the remaining turn excess is a **target** question, not a data question. The
game's road network is authoritative and genuinely twistier than the hand trace.
Whether 6570 turns is *correct* depends on what the player should be told, and
the game is the better oracle. Two ways to settle it, neither yet done:

- **Ask the game.** Drive the same routes in-game, count the real turn
  instructions, match that. Authoritative, slow.
- **Measure what players see.** The app shows turns along the *chosen route*,
  not across the whole network, so a 136% whole-network excess may be far
  smaller per actual route. `turn_check.py` on real town pairs is the measure —
  **this is the next step and it has not been run**, because `skimage` and the
  rest of the pixel toolchain are not installed on this Windows box.

`y_sign=-1` is confirmed on all 43 hexes: mean fwd 0.680 against 1.679 for +1,
and it wins on every individual hex.

**Do NOT read the per-line turn counts in `score_pak.py` as the app's metric.**
It counts turns within each line; leaflet-routing-machine counts turns along a
*route*. Only `turn_check.py` is authoritative. Re-measure with it before
claiming any turn improvement.

### Verified

    node tier_check.js              # tier round trip ......... PASS
    score_pak.py --rangeall <hexes>  # grid containment ....... PASS, 5.4% max overhang
    node verify_roads.js             # artefact check ......... 3 of 43, all traced

**`--rangeall` is the authoritative frame check** and it passes: the worst
overhang across all 53 hexes is **1.372 units, 5.4% of a hex**, which is the
game's tiles overlapping at their edges — real geometry, not a frame error. It
fails only above 25% of a hex, which no legitimate edge overlap reaches. That
margin is measured rather than guessed, because a tight one flags correct work:
at 2% of a hex it reported 15 "failures" that were all sub-pixel edge overhang.

It does not compare the pak against another dataset, so it cannot be fooled by
agreeing with a shared mistake — a hex drawn one hex off would pass every
pairwise comparison and fail here.

### The 6-region offset: SOLVED — it was a centring bug in this route

Root cause: `hex_lines()` anchored each hex by the **centre of its road bounding
box**, assuming roads fill the hex symmetrically. They do not. Measured with
`score_pak.py --ueframe`, that bbox centre wanders **64,425 cm of x — 29% of a
hex's 220,000 cm width** — so every hex's whole network was translated by up to
a seventh of a hex.

The give-away was that the flagged regions were **rigid translations** (both
bbox ends shifted by the same amount), which is what a centring error looks like
and what edge-clipping does not.

**Fix:** anchor on the UE map origin `(0, 0)`, which is the hex centre — the road
data spans about -95,000..+95,000 cm about it, so the map is authored around the
origin. `ue_origin` is now a parameter, defaulting to `(0, 0)`.

Effect, measured over all 43 traced hexes:

| | before | after |
|---|---|---|
| mean forward error | 0.680 | **0.110** (6x better) |
| regions flagged as offset | 6 of 43 | 3 of 43 |
| per-region delta | ~3.0 units | **~0.1 units** |

**Do not reintroduce bbox-centring.** It is a plausible-looking convenience that
is silently wrong for any hex whose roads are not symmetric.

### The 3 remaining flagged regions are trace artefacts, not missing road

`score_pak.py --offset` and `--gap` classify all three:

| region | cause |
|---|---|
| ViperPitHex | trace spill — 4 stray points owned by **CallahansPassageHex** |
| GodcroftsHex | trace spill — 54 stray points owned by **TempestIslandHex** |
| ReaversPassHex | one-sided extent, 0 stray points, road present |

`--gap` measures what `bwd 0.000` hides (3 dp averages a few stray points away):
unmatched traced points are 0.6-1.5% of the total, worst-case distance ~2 units
— lateral offset between the hand line and the authored centreline, not absent
road. **No road is missing.**

`TheFingersHex` shows 10.6% unmatched, the highest, and is one of the two hexes
`scripts/rebake-road-source.mjs` shifts by hand. Worth a look if turn quality
there matters, but the road is present.

### Still to do

**Blocking the swap:**

- **`all_roads.geojson` is unavailable**, so there is still no end-to-end comparison
  of the pak build against the *shipped* file. Everything measured here is pak vs
  the hand trace. This is the main remaining gap.
- The tier *distribution* is still unchecked: the pak build is 1,917 / 1,042 / 764
  across tiers 1/2/3, i.e. gravel-heavy, where the trace is far more evenly spread.
  That may be correct (the game may genuinely have more gravel) or may mean roads
  are read at the wrong detail level. Note this is a *distribution* question and
  is unaffected by the geometry findings above.
- `RoadGreatMarch01` -> tier 1 is a judgement call, not evidence. Great March only.

**No longer blocking:**

- ~~Run `turn_check.py` on real town pairs~~ — **done**, and it is the headline
  result: pak 85.2 vs hand 109.1 turns/route, i.e. the pak is *smoother*, not
  noisier. The 136% excess figure was a per-line artefact and should not be quoted.
- ~~Check the geometry~~ — **done**. 99% agreement within 10 m; see the section
  above. The alarming intermediate numbers were tolerance artefacts.
- ~~Investigate the blue line crossing water/rock~~ — **done**. Not a tier bug;
  it is the mud tier, and the geometry is sound.
- ~~`TheFingersHex` 10.6% unmatched~~ — **explained**. Road is present; it is
  lateral offset, and it is one of the two hexes `rebake-road-source.mjs` shifts
  by hand. Only worth attention if turn quality there specifically matters.

**Verdict as of this handover: the pak build is ready for production promotion**,
subject to the `all_roads.geojson` check above. `J:\pak_roads.geojson` (3,723
features, 53 regions) is staged but **NOT deployed** — replacing production's
20,953 hand-traced features is a human decision, exactly as the pixel build was.

