"""
Connect road ends across bridge placements.

WHY
---
The pak stores roads as splines and bridges as *placements*.  A road spline stops
at the bank; the bridge carries it over.  The road loader only ever read the
`splines` key, so the deployed network has 3,723 road features and NO bridge
geometry at all: 386 bridge placements across 47 of 53 hexes are simply absent.
Roads that should meet at a crossing are left as two unjoined ends.

A bridge placement is a position, yaw and scale -- not a span -- so drawing the
deck needs the mesh dimensions, which are in the pak and not in the export (the
same unsolved problem as sidewalk piece lengths).  But routing does not need the
deck drawn: it needs the two road ends either side of the water to be in the
same component.  The bridge's POSITION is enough to find that pair.

THE PAIRING RULE, and why it is auditable
-----------------------------------------
For each bridge, take the nearest unused road end on each side and join them if
the span is plausible.  The gap distribution is the check: if the hypothesis is
right, matched spans cluster at real bridge lengths.  A rule that quietly welded
unrelated roads would show as a long tail of implausible spans, so the report
prints the whole distribution rather than a count.

Ends must be on OPPOSITE sides of the bridge.  Without that test a road merely
passing nearby on one bank gets joined across open water, which is the one
thing this must never do.
"""
import argparse
import json
import math
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import grid
import roads_from_pak as rfp
import roads_from_blueprints as rbf

HERE = os.path.dirname(os.path.abspath(__file__))

# Max bridge span in world units; 1 world unit is about 86 m, so this is very
# generous.  The measured span distribution, not this ceiling, is what
# constrains the result.
MAX_SPAN = 10.0

# Train bridges are EXCLUDED from routing, by request.  They are also the worst
# behaved input to this pairing rule: of the 259 joins they contributed 34, and
# they own every span in the long tail (p50 135 m against 66 m for everything
# else, max 763 m).  A train bridge is a rail structure spanning a long
# multi-track gap, not a road crossing, and a vehicle cannot use it -- so
# joining road ends across one would invent a route that does not exist in
# game.  The mesh list is explicit rather than a pattern so a new bridge type is
# added deliberately instead of being swept in by accident.
EXCLUDE_MESHES = (
    "BPTrainBridgeA_C",
    "BPTrainBridgeB_C",
    "BPTrainBridgeC_C",
    "BPTrainBridgeBridge_C",
)


def to_world(region, x_cm, y_cm):
    ox, oy = grid.hex_origin(region)
    s = rfp.cm_to_world_scale()
    return (ox + grid.W / 2.0 + x_cm * s, oy - grid.K / 2.0 - y_cm * s)


def merc_to_world(mx, my):
    """EPSG:3857 -> world units, the inverse of roads.js.

    Defined here rather than imported: roads_from_pak only carries the forward
    direction, and getting the divide/multiply backwards has silently produced
    nonsense more than once in this project.
    """
    k = 128.0 / 20037500.0
    return (mx * k + 128.0, my * k - 128.0)


def world_to_merc(wx, wy):
    """world -> EPSG:3857.  Multiply by MERC_HALF/HALF_WORLD, never divide."""
    s = 20037500.0 / 128.0
    return ((wx - 128.0) * s, (wy + 128.0) * s)



def bridge_points(region):
    _streets, bridges = rbf.collect_pieces(region)
    return [(b["mesh"], *to_world(region, b["x"], b["y"])) for b in bridges]


def pair_bridges(region, lines, max_span=MAX_SPAN):
    """Join road ends across each bridge.  Returns (unions, report rows).

    `lines` is a list of (region, tier, points) in world units.  Returns a list
    of (line_a, line_b) index pairs to union, plus one report row per bridge.
    """
    ends = []
    for i, (_r, _t, pts) in enumerate(lines):
        ends.append((i, 0, pts[0]))
        ends.append((i, 1, pts[-1]))
    used = set()
    unions, rows = [], []
    for mesh, bx, by in bridge_points(region):
        if mesh in EXCLUDE_MESHES:
            rows.append({"mesh": mesh, "gap": None, "status": "excluded"})
            continue
        cands = []
        for i, which, p in ends:
            if (i, which) in used:
                continue
            d = math.hypot(p[0] - bx, p[1] - by)
            if d <= max_span:
                cands.append((d, i, which, p))
        cands.sort()
        if len(cands) < 2:
            rows.append({"mesh": mesh, "gap": None, "status": "too-far"})
            continue
        _d1, i1, w1, p1 = cands[0]
        _d2, i2, w2, p2 = cands[1]
        span = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
        if span > max_span:
            rows.append({"mesh": mesh, "gap": span, "status": "span-too-long"})
            continue
        # Opposite sides of the bridge?  Compute each end's offset along the
        # line joining the two ends; if both have the same sign they are on the
        # same side, i.e. one bank, and joining them would cross open water.
        n = span or 1.0
        ux, uy = (p2[0] - p1[0]) / n, (p2[1] - p1[1]) / n
        s1 = (p1[0] - bx) * ux + (p1[1] - by) * uy
        s2 = (p2[0] - bx) * ux + (p2[1] - by) * uy
        if s1 * s2 > 0:
            rows.append({"mesh": mesh, "gap": span, "status": "same-side"})
            continue
        used.add((i1, w1))
        used.add((i2, w2))
        unions.append((i1, i2))
        rows.append({"mesh": mesh, "gap": span, "status": "joined",
                     "lines": [i1, i2]})
    return unions, rows


def build(in_path, out_path, max_span=MAX_SPAN):
    grid.load_offsets(os.path.join(HERE, "scripts",
                                   "export_major_locations.sh"))
    data = json.load(open(in_path, encoding="utf-8"))
    feats = data["features"]

    # Flatten to world-unit lines, remembering each line's owning feature and
    # sub-line index so the geometry can be written back in place.
    lines, owner = [], []
    for fi, f in enumerate(feats):
        h = f["properties"]["region"]
        g = f["geometry"]
        cs = g["coordinates"] if g["type"] == "MultiLineString" \
            else [g["coordinates"]]
        for si, c in enumerate(cs):
            if len(c) >= 2:
                w = [merc_to_world(p[0], p[1]) for p in c]
                lines.append((h, f["properties"].get("tier"), w))
                owner.append((fi, si))

    by_region = defaultdict(list)
    for i, l in enumerate(lines):
        by_region[l[0]].append(i)

    all_unions, all_rows = [], []
    for region in sorted(by_region):
        idxs = by_region[region]
        u, rows = pair_bridges(region, [lines[i] for i in idxs], max_span)
        all_unions.extend((idxs[a], idxs[b]) for a, b in u)
        for r in rows:
            r["region"] = region
            all_rows.append(r)

    # Move each joined end to the midpoint of the crossing, so the two lines
    # actually meet rather than merely being declared adjacent.  This is the
    # same treatment snap_borders.py gives border crossings.
    mid_of_pair = {}
    for a, b in all_unions:
        pa, pb = lines[a][2], lines[b][2]
        best = None
        for ea in (pa[0], pa[-1]):
            for eb in (pb[0], pb[-1]):
                d = math.hypot(ea[0] - eb[0], ea[1] - eb[1])
                if best is None or d < best[0]:
                    best = (d, ea, eb)
        _d, ea, eb = best
        mid_of_pair[(a, b)] = ((ea[0] + eb[0]) / 2.0, (ea[1] + eb[1]) / 2.0)

    out = [dict(f) for f in feats]
    moved = 0
    for a, b in all_unions:
        mid = mid_of_pair[(a, b)]
        for li in (a, b):
            pts = lines[li][2]
            target = list(pts)
            if math.hypot(pts[0][0] - mid[0], pts[0][1] - mid[1]) <= \
               math.hypot(pts[-1][0] - mid[0], pts[-1][1] - mid[1]):
                target[0] = mid
            else:
                target[-1] = mid
            fi, si = owner[li]
            f = out[fi]
            g = f["geometry"]
            cs = g["coordinates"] if g["type"] == "MultiLineString" \
                else [g["coordinates"]]
            mc = [list(world_to_merc(p[0], p[1])) for p in target]
            if g["type"] == "MultiLineString":
                cs[si] = mc
            else:
                g["coordinates"] = mc
            moved += 1
    data["features"] = out
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    return all_rows, moved, len(feats)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="src", required=True)
    ap.add_argument("--out", dest="dst", required=True)
    ap.add_argument("--max-span", type=float, default=MAX_SPAN,
                    help="max bridge span in world units (%.0f is about "
                         "%.0f m)" % (MAX_SPAN, MAX_SPAN * 85.9))
    ap.add_argument("--report", help="write per-bridge JSON here")
    a = ap.parse_args()
    if os.path.abspath(a.dst) == os.path.abspath("/tmp/rx/snapped.geojson"):
        raise SystemExit("refusing to write the deployed file")
    rows, moved, n = build(a.src, a.dst, a.max_span)
    counts = defaultdict(int)
    gaps = []
    for r in rows:
        counts[r["status"]] += 1
        if r["status"] == "joined":
            gaps.append(r["gap"])
    gaps.sort()
    print("bridges examined: %d" % len(rows))
    for k in sorted(counts):
        print("   %-16s %d" % (k, counts[k]))
    if gaps:
        print("joined span distribution (world units, 1 u = 86 m):")
        for q in (10, 25, 50, 75, 90):
            v = gaps[min(len(gaps) - 1, int(len(gaps) * q / 100))]
            print("   p%-3d %6.2f u = %5.0f m" % (q, v, v * 85.9))
        print("   max %6.2f u = %5.0f m" % (gaps[-1], gaps[-1] * 85.9))
    print("wrote %s (%d features, %d line geometries rewritten)"
          % (a.dst, n, moved))
    if a.report:
        json.dump(rows, open(a.report, "w"), indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())




