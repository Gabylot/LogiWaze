# Road network — investigation handoff (Linux agent → Windows agent)

**From:** the Linux box (`/var/www/LogiWaze`, `/var/www/Hermes`)
**Date:** 2026-09-28
**Ask:** one question, answerable only on the box that still has `PAK_JSON`.
Everything below is measured; nothing is speculation unless marked.

---

## The one question I need answered

The extracted road arrives as **327 separate chunks**. The gap between the
nearest ends of two *different* chunks has a **median of 270 m** (p90 543 m,
max 1520 m). At 6,943 of those gap midpoints, the **hand-traced** network has
road within a **median of 24 m** (33% within 10 m, 84% within 100 m).

So the hand trace joins those chunks with road; the pak data does not have it.

**Question:** pick any of those gaps and look in the exporter output for that
location. Does `splines` contain road there?

- **If yes** → the extractor is dropping road it was given (a length filter, a
  mesh family outside the 9 names, a bounds rejection). This is an extraction
  recall bug and I can fix it downstream.
- **If no** → the hand trace contains road the game does not have. The tracer
  drew from map *pixels*, so it can draw a road that looks continuous on the
  map but is not drivable mesh. If that is the case the pak data is correct,
  the 327 chunks are real, and every "route improvement" I have shipped is a
  phantom road invented to bridge them.

I cannot distinguish these from here. The second case is the one I now think is
more likely, and it would mean this whole line of work is solving a problem
that does not exist.

**Useful for the test:** `pak_acceptance.py` and `NETWORK_FINDINGS.md` §8 both
describe `roads_from_blueprints.py` inferring street-piece length from pooled
nearest-neighbour spacing, with per-mesh lengths spanning 206–2127 cm inside
one hex. A mislocated 20 m piece would look exactly like my "missing 20 m
connector" at the Salt Farms junction. Worth ruling out first.

---

## What is deployed right now

```
/var/www/Hermes/public/logiwaze/index.html
road_source.geojson  md5 5ec2f422c8d503f1665f8fe3e65431dd
rollback:  /root/*.20260928-224147
```

This is the build that fixed Salt Farms → Thunderfoot (7.54 → 2.05 km). It is
**better than the hand trace on every metric I have**, and it has 27 phantom
road segments over 150 m (11 of them in FarranacCoast, which the human spotted as
"diagonal roads where there shouldn't be any").

**Nothing from the last few hours is deployed.** The only files you should
trust as live are the ones above.

---

## Where the numbers stand

| | deployed | hand trace |
|---|---|---|
| pairs routable (1,849 sample) | 74.4% | 74.4% — *the same 1,375 pairs* |
| vs hand trace, distance | +4.8% | — |
| segments >150 m | **27** | 0 |

Over the session routability went 59.4% → 74.4% and the distance penalty
against the hand trace went +31.1% → +4.8%. All of that came from **bridging
the 270 m chunk gaps**, which is also where the phantom diagonals come from.


---

## Tools built (all in `/var/www/LogiWaze`, uncommitted)

| file | what it does |
|---|---|
| `graphkit.py` | the graph model: components, near-misses, join classification |
| `test_graphkit.py` | 11 tests, all passing — each pins a mistake I made |
| `graph_audit.py` | structural health: components, near-miss gaps, town reach |
| `join_roads.py` | **new joiner**, replaces the weld/split pair (see below) |
| `route_test.py` | named route checks; models the real browser router |
| `benchmark.py` | 1,849-pair routability/distance gate used before every deploy |
| `snap_coincident.py` | merges vertices within 20 mm |
| `weld_network.py`, `split_network.py` | superseded by `join_roads.py` |

### The frame, settled — do not re-derive

- Network files are **EPSG:3857**; `towns.json` is the app's town frame, offset
  by **(128.25, -128.25)** — *not* `grid.WORLD_ORIGIN`, which puts towns a
  hex-width from their own roads.
- `grid.hex_origin()` returns the hex **top-left**; centre is `(+W/2, -K/2)`.
- The hex grid packs diagonally at **0.75·W**, not W/2. Assuming W/2 makes 90
  genuinely adjacent hex pairs look non-adjacent.
- **Gate any town↔road assertion on a containment check first.** Both of us
  published distances to roads in the wrong hex before that guard existed.
- The browser router (`IRouter.ts:1024`) snaps a waypoint to the nearest
  **waypoint** in `L.geoJSON(Paths)`, then runs the geojson-path-finder, which
  keys nodes with `Math.round(c/1e-5)*1e-5`. That is **not** decimal rounding —
  modelling it with `round(x,6)` gave different answers.

### `join_roads.py`

Decides a whole round of joins against the current geometry, applies them all
at once, then rebuilds and repeats. That ordering is the point: inserting
vertices one at a time invalidates every index computed earlier, and that bug
produced the 180–286 m phantom diagonals. Converges in 2 rounds / 320 joins.

It reaches only 66.7% routable because it *refuses* to invent the 270 m links.
The deployed 74.4% build gets there by inventing them.

---

## What I got wrong, so it isn't repeated

- Chased the hex border for several turns after being told the border was fine.
- Measured endpoint gaps at **19.6 m** and reported that as "the" gap. It is the
  median to the nearest end *of any kind*; 93% of those pairs are already in the
  same component. The figure that matters is **270 m** between components.
- Classified whole lines as north–south by their start-to-end direction, which
  missed a genuine N–S connector because the line was a U shape.
- Used a 20 mm tolerance to test whether two vertices were "joined" and called
  19.4 mm a hit. That is precisely *not* a hit, and it was the Salt Farms bug.
- Nearly shipped a connectivity regression twice by checking geometry (phantom
  segment counts) before checking routes. The route gate is the one that matters.

---

## What I need, concretely

1. **The gap question above** — exporter `splines` at a 270 m chunk gap: present
   or absent? Present → extraction recall bug I can fix. Absent → stop, the
   hand trace is aspirational and we should not be matching it.
2. Whether `roads_from_blueprints.py`'s pooled piece-length inference (§8 of
   your findings) is misplacing short pieces.
3. `RoadGreatMarch01Snow` is still dropped — 10 segments, real but minor, and
   it cannot explain TerminusHex.

If (1) comes back "absent", the honest recommendation is to **stop optimising
against the hand trace** and ship the pak network as-is with the phantom joins
disabled, accepting ~66.7% routability and correct geometry. That is a product
call, not a technical one, and I would rather make it deliberately than keep
manufacturing road.

---

# 2026-09-29 — exporter on Linux, and three corrections

## The pak IS readable on this box

`War-WindowsNoEditor.pak` (23.8 GiB) is not encrypted in a way that blocks us.
The blocker was only that `roads_from_pak.py` never read the pak directly: it
wants `export/_json/<Hex>.json` from Tsekho/fh_map_exporter, which is C#/.NET.

    git clone --depth 1 --recurse-submodules \
        https://github.com/Tsekho/fh_map_exporter.git
    # Exporter.csproj is pinned to win-x64; change to linux-x64
    dotnet build -c Release        # succeeds, 0 errors, CUE4Parse-Natives
                                  # skipped (no cmake) and that is fine
    dotnet bin/Release/net10.0/linux-x64/Exporter.dll \
        -i /var/www/LogiWaze/War-WindowsNoEditor.pak \
        -o /tmp/rx/fh_export -a War/Content/Maps/Master/<Hex>

**52 of 55 hexes export** (~13 s each, 741 MB). `HomeRegionC`, `HomeRegionW` and
`MarbanHollowHex` do not exist under `War/Content/Maps/Master` and cannot be
exported; they are the 3 hexes the old hand-supplied pak file did not have
either. `PAK_JSON_DIR=/tmp/rx/fh_export/_json` then feeds `roads_from_pak.py`
unchanged, giving 4,510 features. **The pak path is reproducible on Linux.**

## Why big cities have no pak road  (confirmed, and it is not a filter bug)

    town              hex               pak_fresh    hand-traced
    Therizó           TerminusHex        110.8 m       5.8 m
    The Jade Cove     FarranacCoastHex   219.9 m      14.9 m

Reproduced from a fresh export, so it is not a stale-file artefact. Outside
towns, road is spline meshes and is plentiful (FarranacCoast: 1,248 entries,
12.3 km). Inside towns the road surface is **not a road spline at all** — it is
`TownSidewalk01`–`06`, `TownSidewalkCorner*` and the `TownW*` building set, and
those names appear only under `symbols` and `blueprints`, never `splines`.
`classify()` reads `splines`, so town road is structurally invisible to it.

To route big cities from the pak alone, town geometry has to be read as road,
which is a different code path from `hex_lines` (meshes, not splines).

## Two things I got wrong, reverted, with the evidence in the code

Both were the same mistake, so both are recorded in `grid.py` and
`weld_network.py` so they are not repeated.

1. **`WORLD_ORIGIN` was "corrected" to (128.047, -128.048) and reverted.**
   A least-squares refit of per-hex road centroids over 53 hexes proposed it,
   and it is alluring because it is exactly the `HALF` in `graphkit.m2w`.
   Measured against the deployed hand-traced road:

       origin (115.21725, -116.90975)   pak road sits 0.05 units from it
       origin (128.04726, -128.04798)   pak road sits 7.70 units from it

2. **`Hexes.observed_centres` override added, then removed.** It let pak-derived
   hexes override the grid layout, and cost 127 legitimate cross-hex welds,
   every one rejected `non-adjacent-hex`.

The shared flaw: I used the **median road vertex as a proxy for hex centre**.
Road is not distributed symmetrically inside a hex, so that statistic carries
each hex's road bias and reads as placement error. Prefer an agreement test
against data known to be correctly placed (as in (1)) over any centroid
regression.

## Bugs fixed in the shared tools

- `weld_network.py` write-back raised `IndexError`: 21,273 features vs 21,254
  lines, because `gk.load` drops lines shorter than two points. Walking
  features with one running index ran off the end, and would have handed a
  short feature the *next* feature's geometry. Now realigned per feature;
  degenerate lines pass through untouched.
- `graphkit.near_misses` called `cKDTree.query(..., k=kk)` for the whole network
  at once — a 1.14 GiB allocation that fails outright on a small box — and grew
  `kk` 16→64→256 without bound, so each call took minutes. Now chunked
  (`CHUNK = 2048`) and capped at `kk = 64`. Same results, seconds per call.
  This is why welding previously looked "stuck"; it was not slow logic.

## Where the automated build actually stands

| build | hexes | connected | automated |
|---|---|---|---|
| live (deployed) | 53 | 90.7% | no — hand-trace artifact |
| pak + weld @0.25 | 54 | 15.9% | yes |
| OCR only | 53 | 47.3% | yes |
| OCR + weld @0.5 | 53 | 47.3%, 0 welds | yes |

Nothing automated beats the hand trace yet, so nothing was deployed. Live is
unchanged: md5 `ab774203e3ff5b135bcc2eb2dbd6eb8a`, 11/11 tests green.

**Next step, precisely located.** The 84 remaining OCR endpoint components
present 27,293 cross-component candidate pairs. The nearest ones have gap
~0.0000 yet sit 38–57 units from any shared hex border, while one legitimate
crossing (`CallahansPassageHex`→`ReachingTrailHex`) measures 0.016. So most
candidates are coincident endpoints in different hexes that are *not* border
crossings, and the `far-from-border` guard is right to reject them. Before
loosening it, find out why two hexes' road is coincident 40+ units from their
shared edge — that is either duplicated road near borders or hexes whose road
was assigned to the wrong `region`. Until that is explained, any larger
`--border-tol` risks welding unrelated road, which is how the original phantom
diagonals were created.
