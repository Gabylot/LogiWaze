"""
Route-level comparison of two road networks: does routing work, and how far.

WHY THIS IS SEPARATE FROM netdiff / audit_pak
---------------------------------------------
Those compare SHAPES -- how much of one network lies on the other.  A shape metric
can be excellent while routing fails, because routing depends on CONNECTIVITY.
Two networks can agree on 99% of their length and still leave every town
unreachable if the junctions between lines do not join.  The endpoint-to-segment
bug that had this project believing a healthy network had 3,648 components is
exactly that case: geometry fine, connectivity broken.

So this drives the app's actual task: pick real town pairs, snap each town to
the network, find a shortest path, and report whether one exists and how long.
Both networks are scored on the SAME pairs, so a difference is attributable to
the network rather than to which towns happened to be chosen.

FRAME
-----
Both network files are EPSG:3857, the frame scripts/roads.js and Hermes use.
towns.json is in the app's town frame, offset by (128.25, -128.25), so towns are
converted before use.  Distances are reported in metres, converted through the
world frame -- a mercator distance is not a distance, and the scale error is
~40% at Foxhole's latitude.

    python3 route_compare.py
    python3 route_compare.py --pairs 300 --tol 0.12
"""
import argparse
import heapq
import json
import math
import os
import random
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import grid

HERE = os.path.dirname(os.path.abspath(__file__))
PAK = os.environ.get("PAK_ROADS", r"J:\roads_v3_bridged.geojson")
HAND = os.path.join(HERE, "road_source.geojson")
TOWNS = os.path.join(HERE, "towns.json")

TOWN_FRAME = (128.25, -128.25)
MERC_HALF = 20037500.0
HALF_WORLD = 128.0
W2M = HALF_WORLD / MERC_HALF          # mercator -> world
WORLD_M = 2200.0 / grid.W             # metres per world unit, ~85.9

# Join tolerance, world units.  0.12 is about 10 m, which is where real
# post-weld border crossings sit (measured 11-12 m between Allods and Clahstra).
# The 0.05 (4.3 m) previously used missed them and manufactured thousands of
# phantom disconnected components in a network that routes fine.
JOIN = 0.12
TOWN_SNAP_M = 600.0


def load_lines(path):
    d = json.load(open(path, encoding="utf-8"))
    feats = d.get("features", d)
    out = []
    for f in feats:
        r = (f.get("properties", {}) or {}).get("region")
        g = f.get("geometry", {}) or {}
        cs = g.get("coordinates") or []
        polys = cs if g.get("type") == "MultiLineString" else [cs]
        for line in polys:
            if len(line) >= 2:
                out.append((r, [(float(p[0]), float(p[1])) for p in line]))
    return out


def load_towns(path):
    t = json.load(open(path, encoding="utf-8"))
    s = MERC_HALF / HALF_WORLD
    out = []
    for k, v in t.items():
        if not (isinstance(v, dict) and "x" in v and "y" in v):
            continue
        tx = float(v["x"]) + TOWN_FRAME[0]
        ty = float(v["y"]) + TOWN_FRAME[1]
        out.append({"name": v.get("name", k), "region": v.get("region"),
                    "major": v.get("major"),
                    "x": (tx - HALF_WORLD) * s, "y": (ty + HALF_WORLD) * s})
    return out


def point_seg_d2(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    n = dx * dx + dy * dy
    if n <= 0:
        return (px - ax) ** 2 + (py - ay) ** 2
    t = ((px - ax) * dx + (py - ay) * dy) / n
    t = 0.0 if t < 0 else (1.0 if t > 1 else t)
    ex, ey = ax + t * dx, ay + t * dy
    return (px - ex) ** 2 + (py - ey) ** 2


def build_router(lines, join=JOIN):
    """Graph over line vertices.  Returns (verts, adj, base_of_line).

    Vertices are chained along their own line, and each line's two ENDS are
    additionally joined to any other line they lie on.  Ends only: an interior
    vertex is already connected along its own line, and testing every vertex
    against every segment would weld parallel roads that merely run close.

    The join is endpoint-to-SEGMENT, not endpoint-to-endpoint: line ends lie on
    other lines' interiors (a T-junction into a through-road), measured at p50
    0.0 m versus 17.1 m for the nearest other end.
    """
    verts, owner, adj = [], [], defaultdict(set)
    base_of_line = {}
    for li, (_r, pts) in enumerate(lines):
        base_of_line[li] = len(verts)
        verts.extend(pts)
        owner.extend([li] * len(pts))
        for k in range(len(pts) - 1):
            a, b = base_of_line[li] + k, base_of_line[li] + k + 1
            adj[a].add(b)
            adj[b].add(a)

    tol2 = (join / W2M) ** 2
    cell = join / W2M
    seg_index = defaultdict(list)
    seg_k = {}
    for li, (_r, pts) in enumerate(lines):
        for k in range(len(pts) - 1):
            a, b = pts[k], pts[k + 1]
            seg_k[(li, id(a))] = k
            cx0 = int(math.floor(min(a[0], b[0]) / cell))
            cx1 = int(math.floor(max(a[0], b[0]) / cell))
            cy0 = int(math.floor(min(a[1], b[1]) / cell))
            cy1 = int(math.floor(max(a[1], b[1]) / cell))
            if (cx1 - cx0 + 1) * (cy1 - cy0 + 1) > 400:
                continue          # very long segment; its own ends index it
            for cx in range(cx0, cx1 + 1):
                for cy in range(cy0, cy1 + 1):
                    seg_index[(cx, cy)].append((li, a, b))

    for li, (_r, pts) in enumerate(lines):
        base = base_of_line[li]
        for vi in (base, base + len(pts) - 1):
            p = verts[vi]
            cx = int(math.floor(p[0] / cell))
            cy = int(math.floor(p[1] / cell))
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for lj, a, b in seg_index.get((cx + dx, cy + dy), ()):
                        if lj == li:
                            continue
                        if point_seg_d2(p[0], p[1], a[0], a[1],
                                        b[0], b[1]) > tol2:
                            continue
                        # The crossing point is within tol of THIS line's
                        # segment, but the line's nearest VERTEX may be
                        # hundreds of metres away if it has long segments -- a
                        # 301 m cross-edge was produced at a 10 m tolerance that
                        # way.  So add the projected crossing point as a new
                        # vertex and connect to that instead.  It is exactly
                        # where a driver crosses, so the edge length is the real
                        # distance and no diagonal can appear.
                        ob = base_of_line[lj]
                        k = seg_k[(lj, id(a))]
                        if k is None:
                            continue
                        ax, ay = a
                        bx, by = b
                        dx, dy = bx - ax, by - ay
                        n2 = dx * dx + dy * dy
                        t = 0.0 if n2 == 0 else ((p[0] - ax) * dx
                                                 + (p[1] - ay) * dy) / n2
                        t = 0.0 if t < 0 else (1.0 if t > 1 else t)
                        nv = len(verts)
                        verts.append((ax + t * dx, ay + t * dy))
                        owner.append(lj)
                        adj[nv].add(ob + k)
                        adj[ob + k].add(nv)
                        if k + 1 < len(lines[lj][1]):
                            adj[nv].add(ob + k + 1)
                            adj[ob + k + 1].add(nv)
                        adj[vi].add(nv)
                        adj[nv].add(vi)
    return verts, adj, base_of_line, owner


def snap(px, py, lines):
    """Nearest point on any line: (dist_world, line_index, seg_index, t)."""
    best = (float("inf"), None, None, 0.0)
    for li, (_r, pts) in enumerate(lines):
        for k in range(len(pts) - 1):
            ax, ay = pts[k]
            bx, by = pts[k + 1]
            d2 = point_seg_d2(px, py, ax, ay, bx, by)
            if d2 < best[0]:
                dx, dy = bx - ax, by - ay
                n = dx * dx + dy * dy
                t = 0.0 if n <= 0 else ((px - ax) * dx + (py - ay) * dy) / n
                t = 0.0 if t < 0 else (1.0 if t > 1 else t)
                best = (d2, li, k, t)
    return best


def line_vertex_base(lines, li):
    return sum(len(l[1]) for l in lines[:li])


def dijkstra(verts, adj, src, dst):
    dist = {src: 0.0}
    prev = {}
    pq = [(0.0, src)]
    seen = set()
    while pq:
        d, u = heapq.heappop(pq)
        if u in seen:
            continue
        seen.add(u)
        if u == dst:
            break
        for v in adj[u]:
            if v in seen:
                continue
            ux, uy = verts[u]
            vx, vy = verts[v]
            w = math.hypot(ux - vx, uy - vy)
            nd = d + w
            if nd < dist.get(v, float("inf")):
                dist[v] = nd
                prev[v] = u
                heapq.heappush(pq, (nd, v))
    return dist.get(dst), prev


def route(verts, adj, base_of_line, lines, t1, t2):
    """Snap two towns and return (ok, metres).  ok=False if unroutable."""
    d1, li1, k1, u1 = snap(t1["x"], t1["y"], lines)
    d2, li2, k2, u2 = snap(t2["x"], t2["y"], lines)
    if li1 is None or li2 is None:
        return False, None, d1 * W2M * WORLD_M, d2 * W2M * WORLD_M
    # Snap to the closer of the segment's two vertices, so a town in the middle
    # of a long road does not have to travel to either end first.
    pts = lines[li1][1]
    a, b = pts[k1], pts[k1 + 1]
    n = math.hypot(b[0] - a[0], b[1] - a[1])
    t = u1
    src = base_of_line[li1] + k1 + (1 if t > 0.5 else 0)
    pts2 = lines[li2][1]
    a2, b2 = pts2[k2], pts2[k2 + 1]
    dst = base_of_line[li2] + k2 + (1 if u2 > 0.5 else 0)
    d, _prev = dijkstra(verts, adj, src, dst)
    if d is None:
        return False, None, d1 * W2M * WORLD_M, d2 * W2M * WORLD_M
    return True, d * W2M * WORLD_M, d1 * W2M * WORLD_M, d2 * W2M * WORLD_M


def find_bad_joins(lines, join=JOIN, min_deg=35.0):
    """Cross-line joins where the two roads meet at a sharp angle.

    A road end that lands on another line is usually a T-junction: the joining
    road's direction at that point should be roughly PERPENDICULAR to the line it
    lands on, or close to parallel.  A sharp intermediate angle means the two
    roads merely pass near each other and the join is spurious -- it produces a
    diagonal shortcut across open ground that a driver would have to drive.

    Reported rather than filtered: the right cut-off depends on the map, and a
    silent filter would hide which connections it removed.
    """
    verts, adj, base, owner = build_router(lines, join)
    # direction of each vertex along its own line
    def dir_at(vi):
        o = owner[vi]
        start = base[o]
        n = len(lines[o][1])
        k = vi - start
        pts = lines[o][1]
        if n < 2:
            return None
        if k == 0:
            a, b = pts[0], pts[1]
        elif k == n - 1:
            a, b = pts[n - 2], pts[n - 1]
        else:
            a, b = pts[k - 1], pts[k + 1]
        dx, dy = b[0] - a[0], b[1] - a[1]
        m = math.hypot(dx, dy)
        return (dx / m, dy / m) if m else None

    cache = {}
    out = []
    seen = set()
    for u in list(adj):
        for v in adj[u]:
            key = (min(u, v), max(u, v))
            if key in seen or owner[u] == owner[v]:
                continue
            seen.add(key)
            du = dir_at(u)
            dv = dir_at(v)
            if not du or not dv:
                continue
            dot = abs(du[0] * dv[0] + du[1] * dv[1])
            ang = math.degrees(math.acos(max(0.0, min(1.0, dot))))
            # 90 deg = perpendicular (good T-junction), 0 or 180 = parallel.
            # Anything near 45 deg is the diagonal case.
            dist = math.hypot(verts[u][0] - verts[v][0], verts[u][1] - verts[v][1])
            if abs(ang - 45.0) < min_deg or ang < min_deg:
                out.append((ang, dist * W2M * WORLD_M, u, v,
                            lines[owner[u]][0]))
    out.sort(key=lambda t: -t[1])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pak", default=PAK)
    ap.add_argument("--hand", default=HAND)
    ap.add_argument("--pairs", type=int, default=300)
    ap.add_argument("--tol", type=float, default=JOIN,
                    help="join tolerance in world units (%.2f = ~%.0f m)"
                         % (JOIN, JOIN * WORLD_M))
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--same-hex", action="store_true",
                    help="only pairs within one hex")
    ap.add_argument("--show-failures", action="store_true",
                    help="name the pairs the hand trace routes and the pak "
                         "does not (or the reverse)")
    a = ap.parse_args()
    grid.load_offsets(os.path.join(HERE, "scripts",
                                   "export_major_locations.sh"))

    hand_regions = {r for r, _ in load_lines(a.hand)}
    pak_lines_all = load_lines(a.pak)
    pak_regions = {r for r, _ in pak_lines_all}
    towns = load_towns(TOWNS)

    print("pak  : %s" % a.pak)
    print("       %d lines over %d hexes" % (len(pak_lines_all), len(pak_regions)))
    print("hand : %s" % a.hand)
    print("       %d hexes" % len(hand_regions))
    new = sorted(pak_regions - hand_regions)
    print("       %d hexes are pak-only (no hand trace): %s"
          % (len(new), ", ".join(new)))
    print("       -> routing is compared only on the %d SHARED hexes."
          % len(hand_regions & pak_regions))

    shared = hand_regions & pak_regions
    pak_lines = [(r, p) for r, p in pak_lines_all if r in shared]
    hand_lines = load_lines(a.hand)

    # Towns in shared hexes, so both networks are given the same question.
    usable = [t for t in towns if t["region"] in shared]
    print("\n%d towns in shared hexes" % len(usable))

    verts_p, adj_p, base_p, own_p = build_router(pak_lines, a.tol)
    verts_h, adj_h, base_h, own_h = build_router(hand_lines, a.tol)
    print("graph: pak %d vertices, hand %d vertices (tol %.2f u = %.0f m)"
          % (len(verts_p), len(verts_h), a.tol, a.tol * WORLD_M))

    random.seed(a.seed)
    pairs = []
    for _ in range(a.pairs * 12):
        if len(pairs) >= a.pairs:
            break
        t1, t2 = random.sample(usable, 2)
        if t1["region"] == t2["region"]:
            if not a.same_hex:
                continue
        pairs.append((t1, t2))

    okp = okh = 0
    dp = dh = 0.0
    both = 0
    only_p = only_h = 0
    dp_list, dh_list, fails = [], [], []
    for t1, t2 in pairs:
        p_ok, p_m, _d1, _d2 = route(verts_p, adj_p, base_p, pak_lines, t1, t2)
        h_ok, h_m, _e1, _e2 = route(verts_h, adj_h, base_h, hand_lines, t1, t2)
        if p_ok:
            okp += 1
            dp_list.append(p_m)
        if h_ok:
            okh += 1
            dh_list.append(h_m)
        if p_ok and h_ok:
            both += 1
            dp += p_m
            dh += h_m
        elif p_ok:
            only_p += 1
            if a.show_failures:
                fails.append(("pak only", t1, t2, None, h_m))
        elif h_ok:
            only_h += 1
            if a.show_failures:
                fails.append(("hand only", t1, t2, p_m, h_m))
        elif a.show_failures:
            fails.append(("neither", t1, t2, None, None))

    n = len(pairs)
    print("\n%d random town pairs on shared hexes:" % n)
    print("   routable   pak %4d (%.0f%%)   hand %4d (%.0f%%)"
          % (okp, 100.0 * okp / n, okh, 100.0 * okh / n))
    print("   routable on BOTH: %d (%.0f%%)" % (both, 100.0 * both / n))
    print("   pak only %d,  hand only %d" % (only_p, only_h))
    if both:
        print("   mean distance where both route: pak %.0f m, hand %.0f m "
              "(pak is %+.1f%%)"
              % (dp / both, dh / both, 100.0 * (dp - dh) / dh))
        if dp_list and dh_list:
            print("   median distance: pak %.0f m, hand %.0f m"
                  % (sorted(dp_list)[len(dp_list) // 2],
                     sorted(dh_list)[len(dh_list) // 2]))
    if a.show_failures and fails:
        print("\n   pairs that do not route on both:")
        for kind, t1, t2, pm, hm in fails:
            print("      %-9s %-20s (%-18s) -> %-20s (%-18s)%s"
                  % (kind, t1["name"][:20], t1["region"],
                     t2["name"][:20], t2["region"],
                     "" if pm is None else "  pak unroutable"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

