# Pak road network — findings to 2026-09-27, for the Linux agent

Read `ROAD_EXTRACTION_HANDOVER.md` first for the pipeline background. This file
covers only the road-network work done after that handover was written, and ends
with an explicit list of what is unresolved.

Everything below is measured on this Windows box. `PAK_JSON` is
`J:\fh_export\_json` (53 hexes). The Linux box has no pak, so nothing here can be
reproduced there except the hand-trace comparisons.

---

## 1. The headline problem, and what is now settled

The road network was missing whole categories of drivable surface, because
`roads_from_pak.load_splines()` reads **only the `splines` key** of the exporter
output. Each hex JSON has four keys:

    blueprints  dict len=212      <- bridges, town sidewalks, buildings
    groups      dict len=56       <- foliage
    splines     dict len=4        <- the ONLY key the loader reads
    symbols     dict len=234      <- some bridges, props

Bridges and sidewalks are placed as a 9-element transform, not as a spline, so
they need a separate reader. Neither is optional decoration:

| category | placements | hexes | where |
|---|---|---|---|
| road splines | 73,081 segs | 53 | `splines` (read) |
| bridges | 477 | 47 | `blueprints`, `symbols` |
| `TownSidewalk*` | 6,825 | 49 | `blueprints` |

`mesh_survey.py` reproduces those counts. Its original version only scanned
`splines` and so reported "nothing important dropped" — that was a blind spot in
the instrument, not a property of the data.

## 2. Coverage, and the correlation that pointed at the cause

`coverage.py` compares per-hex length, pak vs the hand trace, for all 43 shared
hexes. It also compares the pak's **raw spline length** against what
`hex_lines()` emits, which is the measurement that matters most:

    TerminusHex  raw 1795 entries -> 208.2 u   emitted 208.2 u   loss 0.000%

**The builder loses nothing.** Every spline it is given comes out. So the earlier
shortfall against the hand trace is not a filter bug.

Per-hex coverage is uneven — 34 of 43 at or above 95%, 9 below:

    EndlessShore   79.0%      DeadLands     89.6%
    FarranacCoast  81.3%      Origin        91.9%
    ViperPit       82.3%      Heartlands    92.4%
    CallumsCape    82.9%      MarbanHollow  95.4%
    Godcrofts      82.9%      Clahstra      99.7%
    Terminus       85.3%      AllodsBight   97.8%

Nine hexes exceed 100% (MorgensCrossing 104.3%, TheFingers 111.6%), so this is
selective in both directions, not a uniform loss.

The sub-95% hexes are the ones with the most town sidewalks — CallumsCape 971,
OlavisWake 843, **TerminusHex 761**, Heartlands 500, ViperPit 431, EndlessShore
399, DeadLands 380, FarranacCoast 341. Heartlands has **zero** bridges yet sits at
92.4%, which is what ruled bridges out as the sole cause and pointed at

## 3. Are `TownSidewalk*` placements actually roads?

**Yes — they are the drivable town street surface, despite the name.** This was
challenged twice and is worth recording the evidence for.

1. Sampling the map pixel under the centre of each of TerminusHex's 761 pieces:
   the dominant colour is **(213, 129, 111)**, on 325 of them.
   `extract_routes.py:69` documents that exact RGB as a *road* colour
   ("true (213,129,111) sat 102 red — a real tier3-blend road"), inside the
   verified tier-2/tier-3 window. A kerb or verge would not be that colour.
2. The pieces are full-width and single, and the render lands on the painted
   street surface.
3. Distance from each piece to the nearest road spline: p10 2.7 m, p50 16.4 m,
   p90 43.7 m — a broad spread, i.e. streets distributed through a hex, not a
   tight kerb-parallel offset.

So `TownSidewalk` is Foxhole's name for the street. Placing them at tier 2 is
reasonable; the app's tier vocabulary is 1/2/3 and a street is drivable by
anything. A `source` property records the road/street split per feature.

## 4. What the street work achieved

`roads_from_blueprints.py` reads the blueprint/symbol keys, derives each piece's
two endpoints from its position + yaw, and chains them. Full 53-hex build:

    J:\roads_v2_streets.geojson   3723 road + 3932 street features

Effect on TerminusHex, measured by `pak_acceptance.py` (containment-gated, town
projected onto the nearest **segment**):

| town | before | after |
|---|---|---|
| Therizó | 95 m from a road | **4.2 m** |
| Terminus stranded towns (>50 m) | 7 of 17 | **3 of 17** |
| Pariah | ~10 km | **1.4 m** |
| Martti | ~9.6 km | **0.4 m** |

So the road through Therizó was always in the pak; it is a tier-1 gravel spline
plus the surrounding `TownSidewalk` grid, and it is now extracted.

## 5. Border welding — `snap_borders.py` moved into the repo and fixed

Copied from `J:\` to `E:\LogiWaze-master\`. Three fixes were needed before it
could run here:

1. It carried its own hardcoded copy of the origin constants (`ORIGIN_X/Y`) and
   parsed `MapStitcher/map.xml` itself. Replaced with `grid.py`'s offset table —
   one source of truth, which is what the "+K/2 vs -K/2 sign error" in its
   docstring needed.
2. The adjacency test used `and` where it needs `or`, accepting diagonal pairs
   that share no edge.
3. The write-back assumed every feature was a MultiLineString and reindexed
   lines with a per-hex running counter. The new build emits LineString, and
   `[0]`-indexing the converted line kept only the first vertex. Both fixed.

Result on the street build:

    welded 223 endpoint pairs (446 endpoints) across 223 borders
    report: 457 candidate pairs, 446 welded, 11 left open
    J:\roads_v2_snapped.geojson

Gap distribution p50 = 0.143 world units (12 m), matching the independently
measured ~0.15 figure. So the weld tool is behaving as intended.

## 6. Therizó → The Treasury cannot pass, and should not be in the test

TerminusHex's roads stop **140 m** from the Terminus↔AllodsBight border, versus
2 m for a border that welded. The two hexes are connected only by water, so
there is no road to weld and `snap_borders.py` correctly refused. That acceptance

## 7. UNRESOLVED — Pariah → Martti

**This is the open question, and I need the Linux agent to settle it.**

The human reports this route worked before the street work. I measured it as
failing on all three builds, including the road-only original:

    ROADS ONLY (pak_roads.geojson)   Pariah comp size 1   Martti comp size 1   same=False
    STREETS ADDED                     Pariah comp size 1   Martti comp size 1   same=False
    STREETS + WELDED                  Pariah comp size 1   Martti comp size 1   same=False

PariPeakHex and KuuraStrandHex are genuinely adjacent (dx 19.2, dy 0.5K) and 4
welds applied on their shared border, so it is a real land route. But **Pariah
sits on a component of exactly one line** — a single isolated feature in all
three builds.

Two candidate explanations, and I cannot separate them from here:

- **(a) It never worked**, and the earlier "currently passes" record was read
  off the broken-frame harness. Note the earlier PASS was reported while the
  harness was comparing mercator network coordinates against world-frame town
  coordinates; in that state every town was ~8,500 km away and all towns landed
  in one component, which makes a route test pass trivially. So the old PASS is
  not trustworthy evidence either way.
- **(b) It worked and something regressed.** Nothing in this session modified
  `roads_from_pak.py` or the road-only file, and `pak_roads.geojson` is
  byte-identical to what was already staged, which argues against a regression
  — but the staged file is the *output*, so if an earlier build differed, the
  comparison is against the wrong baseline.

**What would settle it:** on Linux, route Pariah → Martti against whatever
`snapped.geojson` is currently deployed, and report whether it resolves. If it
does, diff that network against `pak_roads.geojson` to find what carries the
PariPeak connection — the answer will be a hex or a mesh family this box is not
reading. If it does not, the original acceptance criterion was simply wrong, as
Therizó → The Treasury was.

## 8. UNRESOLVED — the street piece length is wrong

`roads_from_blueprints.py` infers each piece's length from the median
nearest-neighbour spacing, and applies **one pooled value per hex to every
mesh**. The data contradicts that: there is no single native length.

    TerminusHex  761 pieces  pooled 1056 cm  per-mesh 336 / 950 / 999 / 1213 / 1459
    ViperPitHex  431 pieces  pooled  929 cm  per-mesh 206 / 605 / 632 / 858 /  999
    EndlessShore 399 pieces  pooled 1191 cm  per-mesh 509 / 999 / 1105 / 1276 / 1563
    PariPeakHex   20 pieces  pooled  642 cm  per-mesh 200 / 1083

Different `TownSidewalk` meshes have genuinely different lengths (206–2127 cm
inside one hex), so most pieces are currently drawn at the wrong length.

The Terminus render *looked* right, which confirms frame, orientation and
placement — but it does **not** confirm the length, and I previously said it
did. That was wrong. A correct fix needs the real mesh dimensions, which means
reading the pak asset bounds rather than inferring from spacing; that data is not
in the exporter JSON, so it likely needs the exporter or the pak itself.

This is a candidate cause for the Pariah isolation and should be fixed before
judging it.

## 9. Other gaps found

- `STREET_RE` is anchored `^TownSidewalk`, so it misses a second family:
  `Meshes__Environment__Towns__TownSidewalk01/03/Corner01` — 204 placements
  across 4 hexes. **TerminusHex has none of them**, so this is not what fills
  the town block interiors, but it is a real gap.
- Town block interiors have no street geometry. TerminusHex has no second
  sidewalk family, so the interior lanes are either not placed as walkable
  assets or sit under a name not yet identified.
- `towns.json` has a replacement character: `Theriz\ufffd`. The file is
  mis-decoded. Separately, `towns.json`'s `region` field is **not** authoritative
  — `Whispering Gulch` is in DeadLandsHex and `The Squeeze` in
  CallahansPassageHex, both labelled otherwise. Anything filtering on it is wrong.

## 10. Files

New or moved into the repo, all uncommitted:

    roads_from_blueprints.py   streets + bridges from blueprint placements
    pak_acceptance.py          acceptance harness (segment projection, union-find)
    snap_borders.py            from J:\, fixed to run here
    coverage.py                per-hex length coverage + raw-vs-emitted loss
    mesh_survey.py             mesh name census across all keys

`render_overlay.py` gained `--raw`, `--tier` and `--streets-only`.

Staged outputs, none deployed:

    J:\roads_v2_streets.geojson     3723 road + 3932 street
    J:\roads_v2_snapped.geojson     + 223 welds
    J:\border_report.json           per-crossing report

Useful renders:

    J:\overlays\TerminusHex_STREETS.jpg        streets only, full hex
    J:\overlays\TerminusHex_STREETS_ZOOM.jpg   settlement grid close up

## 11. Frames — settled, do not re-derive

Getting these wrong cost most of the time in this session.

- Network files are **EPSG:3857**; `towns.json` is the app's **town frame**,
  offset by **(128.25, -128.25)** — *not* `grid.WORLD_ORIGIN`, which puts
  Therizó outside its own hex. Then convert to mercator.
- `grid.hex_origin()` returns the hex **top-left**; centre is `(+W/2, -K/2)`.
- world→mercator is a **multiply** by 156,562. Naming the divide-side constant
  as "the scale" made a join tolerance 8,600× too small, which presented as
  "nothing is connected" rather than as a units error.
- 1 map px = 1.07 m. Quote geometric thresholds in metres.
- Lengths are not comparable across frames even where positions are:
  `coverage.py` initially compared mercator hand-trace length against world pak
  length and reported 0.0% coverage for all 43 hexes.
- **Gate any town↔road assertion on a containment check first.** Both of us
  reported a distance to a road in a *different hex* before that guard existed.

## 12. Uncommitted and not deployed

Nothing has been pushed to `road-extraction-from-map-images` since commit
`0743432`. No build has replaced anything deployed; the three hand-traced
backups under `/root` are untouched.
