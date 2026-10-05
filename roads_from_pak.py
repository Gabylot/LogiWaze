"""
Build roads from Foxhole game mesh data (the pak), not from map pixels.

WHY THIS EXISTS
---------------
The pixel extractor (build_roads.py / extract_routes.py) skeletonises painted
centreline pixels, so it follows every wiggle the artist's brush had.  That is
the open defect: ~30% excess turn instructions (122.9 vs 95.0 hand-drawn),
and `eps` cannot fix it because simplification overshoots into a turn deficit.

The game stores roads as *spline meshes*.  Each segment is a cubic Hermite
curve the engine (and vehicle physics) follow, authored by hand.  Reading
those directly gives:

  * geometry that is the designer's spline, not a brush outline;
  * tier taken from the mesh name (RoadT1Dirt01 / RoadT2PackedDirt01 /
    RoadT3Gravel01), so true by construction rather than inferred from colour;
  * a turn count derived from the control points the app will describe.

INPUT
-----
`export/_json/<Hex>.json` from Tsekho/fh_map_exporter reading the pak:

    Exporter.exe -i "<...>\\Foxhole\\War\\Content\\Paks" -o export \
                 -a War/Content/Maps/Master/AcrithiaHex

Only the `splines` key matters.  Spline mesh components serialise as
23-element arrays of cubic Hermite parameters, UE world-space centimetres:

    [0..2] world start pos   [3..5] world start tangent
    [6..8] world end pos     [9..11] world end tangent
    [12]   start roll (rad)  [22]   forward axis

    (offsets/scales at 13..16 / 18..21 ride along but do not move the
    centreline)

COORDINATES
-----------
Two frames, bridged here:

  UE cm   per-hex local frame written by the CUE4Parse tool
  world   the app's frame, hexes on the 10x7 grid

The hex centre in world units comes from grid.py's table (the same
`export_major_locations.sh` offsets the pipeline is built on).  UE->world scale
is the one constant available without guessing: a hex is 2200 m across
(the exporter's heightmaps are 2200x2200 px at 1 m/px) and spans grid.W world
units, so 1 world unit = 2200/W metres.  Scale is therefore exact and only the
origin is fitted -- and it is fitted to the hex centre, so unlike the old
WORIGIN constant it cannot absorb per-hex error.

Y SIGN is derived, not assumed: `calibrate` reports both orientations against
the hand-traced ground truth.

Usage
-----
    python3 roads_from_pak.py calibrate AcrithiaHex       # fit + report
    python3 roads_from_pak.py emit AcrithiaHex [...]      # production GeoJSON
    python3 roads_from_pak.py --merge out.geojson AcrithiaHex
"""
import argparse
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import grid

HERE = os.path.dirname(os.path.abspath(__file__))

# Where the pak export landed (fh_map_exporter's export/_json).
PAK_JSON = os.environ.get("PAK_JSON_DIR", r"J:\fh_export\_json")

# ---------------------------------------------------------------------------
# Tier table.
#
# The game's mesh names and the app's tier numbers are OPPOSITE scales, and the
# direction was confirmed against the app rather than assumed:
#
#   src/Panel.ts reads the runtime breakdown by index, where
#       breakdown[2] -> "Gravel/Paved"   breakdown[1] -> "Dirt"
#       breakdown[0] -> "Mud"
#   and src/geojson-path-finder/topology.ts fills that array as `tier - 1`.
#   So at runtime tier 3 = gravel/paved (best), tier 1 = mud (worst).
#
#   src/scripts/roads.js then applies `(3 - tier) + 1` on the way in, which
#   means road_source.geojson -- what this module writes -- uses the INVERSE
#   convention: tier 1 = best, tier 3 = worst.  That is the convention the
#   hand-traced file already uses (it holds 0..4, mostly 1..3), and the
#   distribution is preserved through the inversion, so writing 1/2/3 here is
#   correct and consistent.
#
# The game names its tiers the other way up (RoadT1Dirt is the worst surface),
# so the mapping below is deliberately reversed.
# ---------------------------------------------------------------------------
TIER_MESHES = {
    1: ("Meshes__Environment__Roads__RoadT3Gravel01",),      # best
    2: ("Meshes__Environment__Roads__RoadT2PackedDirt01",),
    3: ("Meshes__Environment__Roads__RoadT1Dirt01",),       # worst
}
# Snow variants: same tier, same surface, different texture.
TIER_SUFFIXES = ("", "Snow")

# Great March's bespoke road.  Not a general tier; treated as tier 1 (it is a
# paved/gravel-class road) and named explicitly so the choice is visible.
SPECIAL_MESHES = {
    "Meshes__Environment__Roads__RoadGreatMarch01": 1,
    "Meshes__Environment__Roads__RoadGreatMarchEnd01": 1,
}

# The editor's own spline visualisation mesh -- not a road, must never count.
IGNORE_EXACT = {"Engine__Content__EditorLandscapeResources__SplineEditorMesh"}
IGNORE_SUBSTR = ("SplineEditorMesh", "EditorLandscapeResources")

# A hex is 2200 m corner to corner; it spans grid.W world units.
HEX_METRES = 2200.0

# Collinear-vertex collapse — DISABLED by default, and the reason is measured.
#
# The theory was that the game's spline meshes are tessellated for rendering, so
# a straight 10 m run arrives as a chain of collinear segments whose vertices
# each register as a turn.  The sweep refuted it (score_pak.py --sweep, 43 hexes):
#
#     deg   verts    turns   excess   mean_fwd
#     off   68760     6570    136%     0.680
#     0.5   59564     6570    136%     0.680
#     2.0   45766     6579    136%     0.679
#     5.0   33154     6613    137%     0.679
#    20.0   17281     8658    211%     0.679
#
# Removing 33% of the vertices moved the turn count by 9 (0.1%), and pushing the
# threshold further makes turns go UP, not down.  So the vertices are not
# collinear tessellation noise -- they carry real direction changes, and the
# roads genuinely curve more often than the hand trace draws them.
#
# The function is kept because it is the right tool if the input ever does carry
# tessellation, and because the sweep is the evidence for leaving it off.
# Default None = emit the designer's geometry unmodified.
COLLINEAR_DEG = None

# Hermite sampling.
#
# The 23-element array carries cubic Hermite tangents, and sampling them gives
# 49.8 km of road from a hex whose segments only span ~14 km -- the tangents
# bow the curve 3.5x beyond the real geometry.  UE spline mesh tangents live in
# component space, and the exporter writes them alongside world-space
# positions, so they cannot be used to reconstruct a world-space curve without
# the component transform.  The knots (start/end positions) ARE world space and
# are the designer's own control points, so the centreline is the chord between
# them.  Measured: chord-only 14.2 km / 51 turns, Hermite 49.8 km / 290 turns.
#
# So SAMPLES_PER_SEGMENT is 1: emit knots only.  This is also what keeps the
# turn count honest -- interior samples on an already-straight segment are pure
# noise, and excess turns are the defect this module exists to remove.
SAMPLES_PER_SEGMENT = 1

# Chaining tolerance, in UE centimetres.
#
# Two spline entries meeting at a junction share a knot to within float noise
# (well under a cm), while the shortest real segment in the data is ~360 cm and
# the median is ~1000 cm.  The tolerance therefore sits far below the shortest
# segment: three orders of magnitude above the noise, one order below the
# geometry.  Chaining runs in cm, BEFORE the transform to world units, because
# a hex is only grid.W (25.6) world units across -- a "0.35" tolerance in world
# units is 3000 cm, which welds unrelated roads together and produces chains
# that double back on themselves.  That bug showed up as ~270 invented turns
# per hex against 28 real ones.
CHAIN_TOL_CM = 25.0



def classify(name):
    """Return the app's tier for a spline mesh name, or None to ignore it."""
    if name in IGNORE_EXACT or any(s in name for s in IGNORE_SUBSTR):
        return None
    if name in SPECIAL_MESHES:
        return SPECIAL_MESHES[name]
    for tier, stems in TIER_MESHES.items():
        for stem in stems:
            for suf in TIER_SUFFIXES:
                if name == stem + suf:
                    return tier
    return None


def cm_to_world_scale():
    """UE centimetres -> app world units.  Exact, from the hex's real size."""
    return grid.W / (HEX_METRES * 100.0)


def load_splines(region):
    """Return [(tier, entry), ...] for one hex, entries in UE cm."""
    path = os.path.join(PAK_JSON, region + ".json")
    if not os.path.isfile(path):
        raise SystemExit(
            "%s: no pak JSON at %s\n"
            "  Run the exporter first:\n"
            '    Exporter.exe -i "<Foxhole>\\War\\Content\\Paks" -o export '
            "-a War/Content/Maps/Master/%s" % (region, region))
    data = json.load(open(path, encoding="utf-8"))
    out = []
    for name, entries in data.get("splines", {}).items():
        tier = classify(name)
        if tier is None:
            continue
        for e in entries:
            out.append((tier, e))
    return out


def hermite(p0, p1, t0, t1, n):
    """Sample a cubic Hermite segment.  p0/p1/t0/t1 are (x, y)."""
    pts = []
    for i in range(1, n + 1):
        t = i / float(n)
        t2 = t * t
        t3 = t2 * t
        h00 = 2 * t3 - 3 * t2 + 1
        h10 = t3 - 2 * t2 + t
        h01 = -2 * t3 + 3 * t2
        h11 = t3 - t2
        pts.append((h00 * p0[0] + h10 * t0[0] + h01 * p1[0] + h11 * t1[0],
                    h00 * p0[1] + h10 * t0[1] + h01 * p1[1] + h11 * t1[1]))
    return pts


def is_straight(p0, p1, t0, t1):
    """True when a segment is a straight run."""
    ax, ay = p1[0] - p0[0], p1[1] - p0[1]
    L = math.hypot(ax, ay)
    if L < 1e-6:
        return True
    ux, uy = ax / L, ay / L
    # Tangent deviation from the chord, relative to segment length.
    dev = max(abs(t0[0] * uy - t0[1] * ux), abs(t1[0] * uy - t1[1] * ux))
    return dev / L < 0.002


def segment_points(e):
    """The centreline knots of one 23-element spline entry, in UE cm.

    Start/end positions only.  The tangents in the array are component-space,
    so sampling the Hermite bows the curve well past the real geometry (see
    SAMPLES_PER_SEGMENT).
    """
    return [(e[0], e[1]), (e[6], e[7])]



def chain(pieces, tol=CHAIN_TOL_CM):
    """Join pieces sharing endpoints, so junctions are not fake turns.

    Runs on raw UE centimetres (see CHAIN_TOL_CM for why).  Chaining is per
    tier: a junction where a gravel road meets a dirt road is a real tier
    change, and the app's tier-aware costing expects those to stay separate
    features.

    A degree-1 endpoint (a dead end) simply stops extending.  A node of degree
    3 or 4 is a real fork, and the chain takes one branch; the remaining pieces
    become their own lines, which is what the app needs -- a fork is where a
    turn instruction lives, so it must not be smoothed through.
    """
    out = []
    used = [False] * len(pieces)
    for i, (tier, pts) in enumerate(pieces):
        if used[i]:
            continue
        used[i] = True
        line = list(pts)
        extended = True
        while extended:
            extended = False
            for j, (t2, p2) in enumerate(pieces):
                if used[j] or t2 != tier:
                    continue
                if near(p2[0], line[-1], tol):
                    line.extend(p2[1:])
                elif near(p2[-1], line[-1], tol):
                    line.extend(reversed(p2[:-1]))
                elif near(p2[-1], line[0], tol):
                    line = p2[:-1] + line
                elif near(p2[0], line[0], tol):
                    line = list(reversed(p2)) + line
                else:
                    continue
                used[j] = True
                extended = True
                break
        out.append((tier, dedupe(line, tol)))
    return out


def hex_lines(region, y_sign=-1.0, y_offset=0.0, collinear=COLLINEAR_DEG,
              ue_origin=None):
    """Road polylines for one hex, in world units.

    `ue_origin` is the UE-frame point that maps to the hex's grid centre.
    Default (0, 0) -- the UE map origin, which IS the hex centre.

    It was previously the CENTRE OF THE ROAD BOUNDING BOX, on the assumption
    that roads fill the hex symmetrically.  Measured (`score_pak.py --ueframe`),
    that assumption is false: the bbox centre wanders over 64,425 cm of x, 29%
    of a hex's 220,000 cm width, so anchoring to it translates each hex's whole
    network by up to a seventh of a hex.  That is the cause of the 6 regions
    verify_roads.js flagged as offset -- they were rigid translations, the
    signature of a centring error rather than missing road.

    The road data spans roughly -95,000..+95,000 cm about the origin, i.e. the
    map is authored around (0, 0), so (0, 0) is the correct anchor.

    `collinear=None` disables the collapse, which the sweep uses for a
    before/after on identical input.
    """
    s = cm_to_world_scale()
    ox, oy = grid.hex_origin(region)
    cx, cy = ox + grid.W / 2.0, oy - grid.K / 2.0

    entries = load_splines(region)
    if not entries:
        return []
    entries = entries + load_bridges(region, entries)
    if ue_origin is None:
        ue_origin = (0.0, 0.0)
    ux, uy = ue_origin

    def to_world(px, py):
        return (cx + (px - ux) * s, cy + y_sign * (py - uy) * s + y_offset)

    # Chain in cm, then transform.  Doing it the other way round would snap at
    # 3000 cm and fabricate junctions (see CHAIN_TOL_CM).
    cm_pieces = [(tier, segment_points(e)) for tier, e in entries]
    out = []
    for tier, line in chain(cm_pieces):
        if collinear is not None:
            line = collapse_collinear(line, collinear)
        out.append((tier, [to_world(*p) for p in line]))
    return out


def world_to_merc(x, y):
    """World units -> EPSG:3857, the inverse of scripts/roads.js."""
    HALF_WORLD = 128.0
    MERC_HALF = 20037500.0
    SCALE = HALF_WORLD / MERC_HALF
    return ((x - HALF_WORLD) / SCALE, (y + HALF_WORLD) / SCALE)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["calibrate", "emit", "hexes"])
    ap.add_argument("hexes", nargs="+")
    ap.add_argument("--merge", help="write a merged road_source.geojson here")
    a = ap.parse_args()
    grid.load_offsets(os.path.join(HERE, "scripts", "export_major_locations.sh"))

    if a.mode == "hexes":
        # The full 10x7 grid, from the authoritative offset table -- NOT from
        # road_source.geojson, which only covers the 43 hand-traced hexes.
        for r in sorted(grid.OFFSETS):
            print(r)
        return 0

    if a.mode == "calibrate":
        # Report both orientations, so the sign is evidence, not assumption.
        for hx in a.hexes:
            for sign in (-1.0, 1.0):
                lines = report(hx, y_sign=sign)
                print("  y_sign=%+.0f  road length %.1f km"
                      % (sign, km_of(lines)))
        return 0

    made = {}
    for hx in a.hexes:
        made[hx] = hex_features(hx)
        print("%-24s %5d features" % (hx, len(made[hx])), file=sys.stderr)
    if not a.merge:
        json.dump({"type": "FeatureCollection",
                   "crs": {"type": "name",
                           "properties": {"name": "urn:ogc:def:crs:EPSG::3857"}},
                   "features": [f for v in made.values() for f in v]},
                  sys.stdout)
        return 0
    src = json.load(open(os.path.join(HERE, "road_source.geojson")))
    keep = [f for f in src["features"]
            if f.get("properties", {}).get("region") not in made]
    out = keep
    for hx in a.hexes:
        out.extend(made[hx])
    src["features"] = out
    json.dump(src, open(a.merge, "w"))
    print("wrote %s: %d features (%d replaced)"
          % (a.merge, len(out), sum(len(v) for v in made.values())),
          file=sys.stderr)


def hex_features(region, **kw):
    """Production road_source.geojson features for one hex."""
    out = []
    for tier, line in hex_lines(region, **kw):
        if len(line) < 2:
            continue
        out.append({
            "type": "Feature",
            "properties": {"region": region, "tier": tier},
            "geometry": {
                "type": "MultiLineString",
                "coordinates": [[list(world_to_merc(x, y)) for x, y in line]],
            },
        })
    return out


def km_of(lines):
    """Total road length, in km.  Summed per line: flattening every line into
    one point list and measuring that also counts the jump from the end of one
    road to the start of the next (which inflated this to 49.7 km against a
    real 14.2 km)."""
    total = 0.0
    for _, line in lines:
        for i in range(len(line) - 1):
            total += math.dist(line[i], line[i + 1])
    return total * (HEX_METRES / grid.W) / 1000.0


def report(region, **kw):
    lines = hex_lines(region, **kw)
    per = {}
    for tier, line in lines:
        per[tier] = per.get(tier, 0) + 1
    segs = load_splines(region)
    knots = sum(len(segment_points(e)) for _, e in segs)
    print("%-20s %5d spline entries -> %5d chained lines  (tiers %s)"
          % (region, len(segs), len(lines),
             ", ".join("t%d=%d" % kv for kv in sorted(per.items()))))
    print("%-20s %5d control points emitted" % ("", knots))
    return lines


def near(a, b, tol):
    return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol


def dedupe(pts, tol):
    """Drop repeated points left by chaining two pieces at one knot."""
    out = [pts[0]]
    for p in pts[1:]:
        if not near(p, out[-1], tol * 0.5):
            out.append(p)
    return out


# Collinear-vertex collapse.  See COLLINEAR_DEG above for why this exists.


def collapse_collinear(line, deg=COLLINEAR_DEG, min_run=2.0):
    """Drop vertices whose two edges are within `deg` of each other.

    A vertex is only removed when BOTH adjacent edges are at least `min_run`
    long, so short jogs at a junction -- where the geometry is real -- are
    never mistaken for tessellation.
    """
    if len(line) < 3:
        return line
    cos_lim = math.cos(math.radians(deg))
    out = [line[0]]
    i = 0
    n = len(line)
    while i < n - 1:
        j = i + 1
        # Extend the current straight run as far as it stays collinear.
        while j < n - 1:
            ax = line[j][0] - out[-1][0]
            ay = line[j][1] - out[-1][1]
            bx = line[j + 1][0] - line[j][0]
            by = line[j + 1][1] - line[j][1]
            la = math.hypot(ax, ay)
            lb = math.hypot(bx, by)
            if la < min_run or lb < min_run:
                break
            cross = ax * by - ay * bx
            if abs(cross) / (la * lb) > math.sin(math.radians(deg)):
                break
            if (ax * bx + ay * by) / (la * lb) < cos_lim:
                break
            j += 1
        out.append(line[j])
        i = j
    return out




# ---------------------------------------------------------------------------
# Bridges.  Appended rather than spliced in: fh_map_exporter emits bridges as
# 9-element transforms under `blueprints` / `symbols`, NOT as splines, so
# load_splines() never sees them and every bridge in the game was being dropped.
# ShackledChasmHex alone holds BPConcreteBridge_C, BPDrawbridgeB/C_C,
# BPPlatformBridge_C and BPTrainBridgeA/C_C; `splines` there contains none of
# them, which is why a crossing ~300 m SE of The Vanguard had no road over it.
#
# A placement gives a position and a yaw but no length, and the spacing between
# placements does not give one either - they are scattered, p50 5.7 m between
# BPDrawbridgeB_C instances against a bridge tens of metres long.  The length
# comes from the road network instead: a bridge sits where a road crosses
# water, so the nearest road end behind it and the nearest ahead of it, along its
# own axis, are its abutments.  Where those abutments are already within
# min_span the road reaches the bridge and CHAIN_TOL_CM welding already joins
# them, so those are skipped rather than emitted as a duplicate line.
# ---------------------------------------------------------------------------

import re as _re
import numpy as _np

BRIDGE_MESH = _re.compile(r"bridge", _re.I)


def load_bridges(region, pieces, min_span=500.0, reach=8000.0, angle=35.0):
    """Synthetic 2-point spline entries for bridges that span a road gap."""
    path = os.path.join(PAK_JSON, region + ".json")
    if not os.path.isfile(path):
        return []
    try:
        data = json.load(open(path, encoding="utf-8"))
    except Exception:
        return []
    ends = []
    for _tier, e in pieces:
        ends.extend(segment_points(e))
    if not ends:
        return []
    P = _np.asarray(ends, float)

    out = []
    for key in ("blueprints", "symbols"):
        for name, entries in (data.get(key) or {}).items():
            if not BRIDGE_MESH.search(name):
                continue
            for e in entries:
                t = e.get("_self") if isinstance(e, dict) else None
                if not t or len(t) < 8:
                    continue
                bx, by = float(t[0]), float(t[1])
                yaw = math.radians(float(t[7]))
                ux, uy = math.cos(yaw), math.sin(yaw)
                rel = P - _np.array([bx, by])
                along = rel @ _np.array([ux, uy])
                across = _np.abs(rel[:, 0] * -uy + rel[:, 1] * ux)
                lim = reach * math.tan(math.radians(angle))
                back = along[(along < 0) & (across < lim)]
                fwd = along[(along > 0) & (across < lim)]
                if not len(back) or not len(fwd):
                    continue
                b, f = float(back.max()), float(fwd.min())
                span = b + f
                if span < min_span or span > reach:
                    continue
                a = _np.array([bx, by]) + b * _np.array([ux, uy])
                c = _np.array([bx, by]) + f * _np.array([ux, uy])
                ent = [0.0] * 23          # shaped like a spline so it flows
                ent[0], ent[1] = float(a[0]), float(a[1])   # through chain()
                ent[6], ent[7] = float(c[0]), float(c[1])   # and hex_lines()
                out.append((1, ent))
    return out


if __name__ == "__main__":
    sys.exit(main())
