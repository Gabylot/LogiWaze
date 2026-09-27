"""
Acceptance test for the pak road network.

WHY THIS IS WRITTEN THE WAY IT IS
--------------------------------
Two earlier attempts to test connectivity failed for reasons that had nothing to
do with the data, and both are avoided here:

1. **Project towns onto the nearest SEGMENT, not the nearest endpoint.**  A town
   sitting in the middle of a long road is metres from the road but possibly
   hundreds of metres from either of its two end points.  Measuring to endpoints
   reports such a town as unroutable and produces false failures.

2. **Union-find over endpoints, not a pixel raster.**  Rasterising rounds
   coordinates onto a grid, so two lines that meet exactly in the data can be
   separated by a pixel.  Chaining is done in the data's own units.

Endpoints are joined when they are within a fixed world tolerance, not a pixel
count, so the answer does not depend on how the data happens to be rasterised.
See ROAD_EXTRACTION_HANDOVER.md: 1 map px is 1.07 m, and quoting a threshold in px
without converting has already produced one wrong conclusion in this project.

    python3 pak_acceptance.py
    python3 pak_acceptance.py --network pak_roads.geojson
"""
import argparse
import json
import math
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HERE = os.path.dirname(os.path.abspath(__file__))
GRID_W = 256.0 / 10.0            # grid.W, world units per hex width
HEX_METRES = 2200.0
WORLD_M = HEX_METRES / GRID_W     # metres per world unit, ~85.9

# towns.json is in the app's TOWN frame, which is offset from the world frame by
# (128.25, -128.25) -- NOT by grid.WORLD_ORIGIN.  Using WORLD_ORIGIN put Therizo
# 1.2 units west of its own hex and made it snap to a road in ShackledChasmHex,
# which is how a 110.8 m measurement became a 15.8 m one.  Both numbers were the
# same bug: a town measured against a road in a different hex.
#
# The constant is verified by CONTAINMENT, not by assumption -- see
# town_is_inside().  Any town->road assertion is gated on that, because a
# plausible-looking distance to the wrong hex's road is worse than no number.
TOWN_FRAME = (128.25, -128.25)

HALF_WORLD = 128.0
MERC_HALF = 20037500.0
# world -> mercator is a MULTIPLY by MERC_HALF/HALF_WORLD (~156,562).  The
# inverse (mercator -> world) divides.  Naming the two separately matters: an
# earlier version used the divide-side value as the scale, which made the join
# tolerance 8600x too small and silently produced 803 components for 806
# features -- every feature isolated, which looks like a connectivity problem
# rather than a units problem.
MERC_SCALE = MERC_HALF / HALF_WORLD
MERC_TO_WORLD = HALF_WORLD / MERC_HALF


def world_to_merc(x, y):
    return ((x - HALF_WORLD) * MERC_SCALE, (y + HALF_WORLD) * MERC_SCALE)


def town_to_merc(x, y):
    """towns.json coordinates -> the network's EPSG:3857 frame."""
    return world_to_merc(x + TOWN_FRAME[0], y + TOWN_FRAME[1])


def town_is_inside(town, region):
    """Is this town inside this hex's world-space box?

    This is the guard that both of us skipped.  Every town/road distance in this
    file is computed only after this returns True, so a town can never be
    measured against a neighbouring hex's road.  Returns (ok, detail) where
    detail explains a failure rather than just returning False.
    """
    import grid
    ox, oy = grid.hex_origin(region)
    x0, y0 = ox, oy - grid.K
    x1, y1 = ox + grid.W, oy
    tx, ty = town["tx"], town["ty"]
    if x0 <= tx <= x1 and y0 <= ty <= y1:
        return True, "inside %s" % region
    # Which hex is it actually in?  Useful when the region label is the thing
    # that is wrong rather than the frame.
    best, bestr = None, None
    for name in grid.OFFSETS:
        bx, by = grid.hex_origin(name)
        if bx <= tx <= bx + grid.W and by - grid.K <= ty <= by:
            best, bestr = name, (bx, by)
    if best:
        return False, ("town is in %s, not %s (hex centres %.1f u apart)"
                       % (best, region,
                          ((bestr[0] - ox) ** 2
                           + (bestr[1] - oy) ** 2) ** 0.5))
    return False, ("town (%.2f, %.2f) is outside %s box x[%.2f,%.2f] "
                   "y[%.2f,%.2f]" % (tx, ty, region, x0, x1, y0, y1))


def to_metres(d_merc):
    """mercator units -> metres, via the world frame.

    Distances in this file are mercator because that is what the network stores.
    Mercator scale inflates distance away from the equator by up to ~40% at
    Foxhole's latitude, so a raw mercator number is NOT a distance and must not
    be reported as one.
    """
    return d_merc * MERC_TO_WORLD * WORLD_M

# Two endpoints closer than this are the same junction.  The network is stored
# in EPSG:3857, so this is a MERCATOR distance, not a world one.  Using 0.05
# world units against mercator coordinates joins almost nothing: 0.05 mercator
# units is under a millimetre, and the run reported 803 components for 806
# features, i.e. nothing connected at all.  0.05 world units is about 4.3 m,
# which is the intended tolerance, so convert it once here rather than at every
# comparison site.
JOIN_PX = 0.05
JOIN_MERC = JOIN_PX * MERC_SCALE

# Route pairs that must be connected.  Pariah -> Martti is a REGRESSION check:
# it passes today and must keep passing, so a change that fixes Terminus by
# breaking connectivity elsewhere is caught.
REQUIRED = [("Therizo", "The Treasury"), ("Pariah", "Martti")]


def load_network(path):
    d = json.load(open(path, encoding="utf-8"))
    return d["features"] if "features" in d else d


def load_towns(path):
    """Towns from towns.json, in BOTH the world frame and the network frame.

    Keeps the world coordinates (tx, ty) because containment is checked in world
    space, and the mercator ones (x, y) because the network is stored in mercator.
    """
    t = json.load(open(path, encoding="utf-8"))
    out = []
    for k, v in t.items():
        if not (isinstance(v, dict) and "x" in v and "y" in v):
            continue
        tx, ty = float(v["x"]) + TOWN_FRAME[0], float(v["y"]) + TOWN_FRAME[1]
        x, y = world_to_merc(tx, ty)
        out.append({"key": k, "name": v.get("name", k),
                    "region": v.get("region"), "major": v.get("major"),
                    "tx": tx, "ty": ty, "x": x, "y": y})
    return out


def lines_of(feats, region=None):
    """Flatten features to [(region, tier, [[x,y],...]), ...]."""
    out = []
    for f in feats:
        p = f.get("properties", {})
        r = p.get("region")
        if region is not None and r != region:
            continue
        g = f.get("geometry", {})
        coords = g.get("coordinates") or []
        polys = coords if g.get("type") == "MultiLineString" else [coords]
        for line in polys:
            if len(line) >= 2:
                out.append((r, p.get("tier"),
                            [(float(a), float(b)) for a, b in line]))
    return out


def point_seg_dist2(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    n = dx * dx + dy * dy
    if n <= 0:
        return (px - ax) ** 2 + (py - ay) ** 2
    t = ((px - ax) * dx + (py - ay) * dy) / n
    t = 0.0 if t < 0 else (1.0 if t > 1 else t)
    ex, ey = ax + t * dx, ay + t * dy
    return (px - ex) ** 2 + (py - ey) ** 2


def build_graph(lines, tol=None):
    """Union-find over the road network.  Returns (find, n_lines).

    THE JOIN IS ENDPOINT-TO-SEGMENT, NOT ENDPOINT-TO-ENDPOINT.  This is the
    single most important thing to get right, and getting it wrong makes a
    healthy network look shattered.

    Measured on the deployed file, over a sample of line ends:
        distance to nearest other line END     p50 17.1 m   p90 69.6 m
        distance to nearest other line SEGMENT p50  0.0 m   p90  0.0 m

    Line ends lie exactly ON other lines' interiors, not on other lines' ends.
    A road that T-junctions into a through-road ends in the middle of it, and
    the through-road was never split at that point.  Endpoint-to-endpoint
    joining therefore finds almost nothing and reports thousands of spurious
    components: 3,648 components for 3,723 lines at a 4.3 m tolerance, which
    looks catastrophically broken and is not broken at all.  Pariah's own line
    is 17.8 m and 56.1 m from its nearest line END but sits directly on a
    neighbour.

    So each line end is joined to whichever line's SEGMENT it lies on, within
    `tol`.  `tol` defaults to JOIN_MERC, in mercator units (the frame the data
    is stored in); 0.05 world units is about 4.3 m.
    """
    if tol is None:
        tol = JOIN_MERC
    n = len(lines)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    cell = max(tol, 1e-9)
    seg_buckets = defaultdict(list)     # cell -> [(line_idx, (a, b))]
    for i, (_r, _t, pts) in enumerate(lines):
        for k in range(len(pts) - 1):
            a, b = pts[k], pts[k + 1]
            lo_x, hi_x = min(a[0], b[0]), max(a[0], b[0])
            lo_y, hi_y = min(a[1], b[1]), max(a[1], b[1])
            nx = int(math.floor(hi_x / cell)) - int(math.floor(lo_x / cell)) + 1
            ny = int(math.floor(hi_y / cell)) - int(math.floor(lo_y / cell)) + 1
            if nx * ny > 64:
                # Very long segment: indexing every cell it crosses would blow
                # up.  Index its own cells only; the caller searches a 3x3
                # neighbourhood, so a long road is still found by its ends,
                # which is where junctions actually occur.
                for cx in range(int(math.floor(lo_x / cell)),
                                int(math.floor(hi_x / cell)) + 1):
                    for cy in range(int(math.floor(lo_y / cell)),
                                    int(math.floor(hi_y / cell)) + 1):
                        if cx in (int(math.floor(lo_x / cell)),
                                  int(math.floor(hi_x / cell))) or \
                           cy in (int(math.floor(lo_y / cell)),
                                   int(math.floor(hi_y / cell))):
                            seg_buckets[(cx, cy)].append((i, (a, b)))
            else:
                for cx in range(int(math.floor(lo_x / cell)),
                                int(math.floor(hi_x / cell)) + 1):
                    for cy in range(int(math.floor(lo_y / cell)),
                                    int(math.floor(hi_y / cell)) + 1):
                        seg_buckets[(cx, cy)].append((i, (a, b)))

    end_buckets = defaultdict(list)
    for i, (_r, _t, pts) in enumerate(lines):
        for p in (pts[0], pts[-1]):
            end_buckets[(int(math.floor(p[0] / cell)),
                         int(math.floor(p[1] / cell)))].append((i, p))

    tol2 = tol * tol
    for (cx, cy), members in end_buckets.items():
        near = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                near.extend(seg_buckets.get((cx + dx, cy + dy), ()))
        if not near:
            continue
        for i, p in members:
            for j, (a, b) in near:
                if i != j and point_seg_dist2(p[0], p[1], a[0], a[1],
                                               b[0], b[1]) <= tol2:
                    union(i, j)
    return find, n



def nearest_segment_index(px, py, lines, keep=None):
    """Index of the line whose SEGMENT is nearest to (px,py), plus distance.

    `keep` optionally restricts to line indices.  Endpoint-only distance would
    report a mid-road town as unroutable, which is the false failure this
    function exists to avoid.
    """
    best, bi = float("inf"), None
    rng = keep if keep is not None else range(len(lines))
    for k in rng:
        pts = lines[k][2]
        for i in range(len(pts) - 1):
            d2 = point_seg_dist2(px, py, pts[i][0], pts[i][1],
                                 pts[i + 1][0], pts[i + 1][1])
            if d2 < best:
                best, bi = d2, k
    return bi, math.sqrt(best) if bi is not None else float("inf")


def _norm(s):
    """Fold a town name for matching: case, and any mangled trailing char.

    towns.json contains 'Theriz\\ufffd' -- a replacement character, i.e. the file
    itself is mis-decoded, not just this script.  Matching on the ASCII prefix
    is the only way to find it.  Non-ASCII is dropped entirely so a name that
    differs only in its corrupted tail still matches.
    """
    s = (s or "").lower()
    return "".join(c for c in s if 32 <= ord(c) < 127).strip()


def find_town(towns, needle):
    """Match a town by name, tolerating the mangling in towns.json."""
    n = _norm(needle)
    exact = [t for t in towns if _norm(t["name"]) == n]
    if exact:
        return exact[0]
    starts = [t for t in towns if _norm(t["name"]).startswith(n)]
    if starts:
        return starts[0]
    loose = [t for t in towns
             if n and (n in _norm(t["name"]) or _norm(t["name"]) in n)]
    return loose[0] if loose else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--network", default=os.environ.get(
        "PAK_ROADS", r"J:\pak_roads.geojson"))
    ap.add_argument("--towns", default=os.path.join(HERE, "towns.json"))
    ap.add_argument("--min-coverage", type=float, default=95.0)
    ap.add_argument("--join", type=float, default=JOIN_MERC,
                    help="endpoint join tolerance in MERCATOR units, the "
                         "frame the network is stored in (default %.6f, "
                         "which is %.1f m)" % (JOIN_MERC, JOIN_PX * WORLD_M))
    ap.add_argument("--only", help="restrict length coverage to one hex")
    ap.add_argument("--focus", help="hex to list unreachable towns for, "
                                     "e.g. TerminusHex")
    a = ap.parse_args()

    feats = load_network(a.network)
    towns = load_towns(a.towns)
    import grid
    grid.load_offsets()          # required by town_is_inside()
    lines = lines_of(feats)
    print("network : %s" % a.network)
    print("          %d features, %d lines, %d regions"
          % (len(feats), len(lines), len({l[0] for l in lines})))

    find, n_lines = build_graph(lines, a.join)
    comps = len({find(i) for i in range(n_lines)}) if n_lines else 0
    print("graph   : %d lines joined endpoint-to-segment at tol=%.6f mercator "
          "(~%.1f m), %d components"
          % (n_lines, a.join, to_metres(a.join), comps))

    # ---- per-hex town reachability, GATED on containment -------------------
    # Restricting to the town's OWN hex is the whole point: a distance measured
    # against a neighbouring hex's road is meaningless, and produced both the
    # 110.8 m and the 15.8 m versions of the Therizo number.
    import grid
    by_region = defaultdict(list)
    for t in towns:
        if t["region"]:
            by_region[t["region"]].append(t)
    lines_by_region = defaultdict(list)
    for i, (r, _t, _p) in enumerate(lines):
        lines_by_region[r].append(i)

    print("\nper-hex town reachability (own hex only, containment-gated):")
    print("%-22s %5s %5s %9s %9s  %s"
          % ("hex", "towns", "ok", "median m", "worst m", "unreachable"))
    print("-" * 92)
    summary = []
    for reg in sorted(by_region):
        tlist = by_region[reg]
        idxs = lines_by_region.get(reg, [])
        gated, rejected, ds = 0, [], []
        far_detail = []
        for t in tlist:
            inside, why = town_is_inside(t, reg)
            if not inside:
                rejected.append((t["name"], why))
                continue
            gated += 1
            if not idxs:
                ds.append(float("inf"))
                continue
            _k, d = nearest_segment_index(t["x"], t["y"], lines, keep=idxs)
            ds.append(d)
            if to_metres(d) > 50:
                far_detail.append((t["name"], to_metres(d), t["major"]))
        if not gated:
            print("%-22s %5d %5d  %s" % (reg, len(tlist), 0,
                                         "no town inside its own hex"))
            continue
        fin = [d for d in ds if d != float("inf")]
        far = [d for d in fin if to_metres(d) > 50]
        med = to_metres(sorted(fin)[len(fin) // 2]) if fin else float("inf")
        wst = to_metres(max(fin)) if fin else float("inf")
        summary.append((reg, len(tlist), gated, med, wst, len(far), len(fin)))
        flag = "  <-- CONTAINMENT" if rejected else ""
        print("%-22s %5d %5d %9.1f %9.1f  %d of %d%s"
              % (reg, len(tlist), gated, med, wst, len(far), len(fin), flag))
        for nm, why in rejected[:3]:
            print("      rejected %s: %s" % (nm[:30], why))
        if far_detail and reg.lower() == (a.focus or "").lower():
            print("      towns over 50 m from their own hex's roads:")
            for nm, d, mj in far_detail:
                print("        %-26s %7.1f m  major=%s" % (nm[:26], d, mj))

    bad = [s for s in summary if s[5] > 0]
    print("\n  hexes with towns >50 m from their own hex's roads: %d of %d"
          % (len(bad), len(summary)))
    print("  towns rejected by the containment gate: %d"
          % sum(len(by_region[r]) - g for r, _t, g, *_ in summary))

    # ---- required routes ---------------------------------------------------
    print("\nrequired routes:")
    ok = True
    for src, dst in REQUIRED:
        ts, td = find_town(towns, src), find_town(towns, dst)
        if not ts or not td:
            print("  %-16s -> %-16s SKIP (town not found)" % (src, dst))
            continue
        ks, ds = nearest_segment_index(ts["x"], ts["y"], lines)
        kd, dd = nearest_segment_index(td["x"], td["y"], lines)
        cs, cd = find(ks), find(kd)
        same = cs == cd
        ok = ok and same
        print("  %-16s -> %-16s %s  %.1f m / %.1f m from road, comp %s vs %s"
              % (src, dst, "PASS" if same else "FAIL",
                 to_metres(ds), to_metres(dd), cs, cd))
    print("\n  routes: %s" % ("ALL PASS" if ok else "FAILING"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

