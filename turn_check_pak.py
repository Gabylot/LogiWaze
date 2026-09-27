"""Turn-instruction check: pak polylines vs hand trace, on real routes.

WHY THIS EXISTS
The 136% "turn excess" in the handover (6,570 vs 2,788) is a PER-LINE count:
it counts every corner on every polyline, whether or not a route ever uses it.
The app never sees it that way.  It routes between two towns and emits turns
only where the chosen path actually bends.  Those are different questions, and
the per-line figure flatters whichever dataset has more, shorter polylines.

So this measures the app's metric.  For each real town pair:
  1. snap both towns to the nearest point on the road network
  2. shortest path over the polyline network (nodes = polyline vertices,
     edges = consecutive vertices, endpoints within `snap` merged)
  3. extract turn instructions from the resulting path with the same
     anchor/probe algorithm and 12-degree tolerance the app uses
  4. report turns per route, pak vs hand

WHY IT IS STANDALONE
turn_check.py builds its reference side from the pixel extractor
(er.extract_hex), so it needs skimage and cannot be pointed at pak data at all.
Both sides here are already GeoJSON, so this needs only numpy + scipy -- no
pixel toolchain, and it runs on this machine.

    python turn_check_pak.py AcrithiaHex DeadLandsHex
    python turn_check_pak.py --all
"""
import argparse
import heapq
import json
import math
import os
import sys

import numpy as np
from scipy.spatial import cKDTree

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import grid

HERE = os.path.dirname(os.path.abspath(__file__))
MAP_PX = 2048

PAK = os.environ.get("PAK_ROADS", r"J:\pak_roads.geojson")
HAND = os.path.join(HERE, "road_source.geojson")
TOWNS = os.path.join(HERE, "towns.json")

TOWN_FRAME = (128.25, -128.25)   # towns.json is in a different world frame
TOWN_SNAP_PX = 600.0
SC = 128.0 / 20037500.0          # mercator -> world

# leaflet-routing-machine's angle buckets
BUCKETS = [(0, 20, 'straight'), (20, 50, 'bear'), (50, 125, 'turn'),
           (125, 170, 'sharp'), (170, 181, 'u-turn')]


def turn_label(deg):
    d = abs(deg)
    for lo, hi, name in BUCKETS:
        if lo <= d < hi:
            return 'straight' if name == 'straight' else (
                name + ('-left' if deg < 0 else '-right'))
    return 'straight'


def turns_of(path, tol_deg=12.0, min_leg_px=10.0):
    """Turns in a densified polyline: anchor/probe, 12-degree tolerance.

    Copied verbatim from route_check.py so the numbers mean the same thing.
    route_check cannot be imported here because it pulls in the pixel extractor.
    """
    if len(path) < 3:
        return []
    pts = [(float(p[0]), float(p[1])) for p in path]
    out, i, n = [], 0, len(pts)
    while i < n - 1:
        base = math.atan2(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1])
        j, found = i + 1, None
        while j < n - 1:
            if math.dist(pts[i], pts[j + 1]) < min_leg_px:
                j += 1
                continue
            b = math.atan2(pts[j + 1][0] - pts[j][0], pts[j + 1][1] - pts[j][1])
            dev = (b - base + math.pi) % (2 * math.pi) - math.pi
            if abs(math.degrees(dev)) >= tol_deg:
                found = (pts[j], turn_label(math.degrees(dev)))
                break
            j += 1
        if found is None:
            break
        out.append(found)
        i = j
    return out



def load_lines(path):
    """{region: [polyline in world units]} for a roads GeoJSON."""
    d = json.load(open(path, encoding="utf-8"))
    per = {}
    for f in d["features"]:
        reg = f.get("properties", {}).get("region")
        g = f["geometry"]
        lines = (g["coordinates"] if g["type"] == "MultiLineString"
                 else [g["coordinates"]])
        for line in lines:
            per.setdefault(reg, []).append(
                [(x * SC + 128, y * SC - 128) for x, y in line])
    return per


def to_px(reg, line):
    """World units -> pixels of that hex's map image (y flipped)."""
    ox, oy = grid.hex_origin(reg)
    s = grid.W / MAP_PX
    return [((x - ox) / s, (oy - y) / s) for x, y in line]


def graph(polylines, snap=1.0):
    """Node graph over consecutive polyline vertices; near endpoints merge.

    A shared endpoint is a junction in the game, so the two segments must
    become one node or no route can ever turn there.
    """
    pts = np.array([p for ln in polylines for p in ln], float)
    if len(pts) < 2:
        return None
    parent = list(range(len(pts)))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    if snap > 0:
        for a, b in cKDTree(pts).query_pairs(snap, output_type='ndarray'):
            ra, rb = find(int(a)), find(int(b))
            if ra != rb:
                parent[rb] = ra

    remap, rep = {}, {}
    for k in range(len(pts)):
        r = find(k)
        remap.setdefault(r, len(remap))
        rep.setdefault(r, pts[k])
    nodes = np.array([rep[r] for r in remap], float)
    adj = [dict() for _ in range(len(nodes))]
    # Step through the original polyline vertex list two at a time, tracking
    # the running vertex index. Assuming len(pts) is even breaks whenever a
    # polyline has an odd vertex count, which raised IndexError; the offset
    # counter is exact for any length.
    k = 0
    for ln in polylines:
        for i in range(len(ln) - 1):
            u, v = remap[find(k + i)], remap[find(k + i + 1)]
            if u != v:
                w = float(math.dist(pts[k + i], pts[k + i + 1]))
                adj[u][v] = min(adj[u].get(v, 1e18), w)
                adj[v][u] = min(adj[v].get(u, 1e18), w)
        k += len(ln)
    return nodes, [sorted(d.items()) for d in adj], pts


def path_from(adj, src, dst):
    dist, prev, pq, seen = {src: 0.0}, {}, [(0.0, src)], set()
    while pq:
        d, u = heapq.heappop(pq)
        if u in seen:
            continue
        seen.add(u)
        if u == dst:
            break
        for v, w in adj[u]:
            nd = d + w
            if nd < dist.get(v, 1e18) - 1e-12:
                dist[v] = nd
                prev[v] = u
                heapq.heappush(pq, (nd, v))
    if dst not in dist:
        return None
    seq = [dst]
    while seq[-1] != src:
        seq.append(prev[seq[-1]])
    return seq[::-1]


def densify(pts, idxs, step=2.0):
    out = []
    for k in range(len(idxs) - 1):
        a, b = pts[idxs[k]], pts[idxs[k + 1]]
        n = max(2, int(math.dist(a, b) / step) + 1)
        for t in np.linspace(0, 1, n):
            out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
    out.append(tuple(pts[idxs[-1]]))
    return out



def per_line_turns(per, hexes):
    """Total turns over every polyline, unrouted. Reference figure only.

    Converted to pixels first: turns_of() has min_leg_px=10, a pixel-scale
    threshold, and world units are ~500x larger, so measuring in world units
    skipped every leg and reported 0 turns for the whole dataset.
    """
    tot = 0
    for r in hexes:
        for ln in per.get(r, []):
            tot += len(turns_of(to_px(r, ln)))
    return tot


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("hexes", nargs="*")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--max-pairs", type=int, default=30)
    a = ap.parse_args()
    grid.load_offsets()
    pk = load_lines(PAK)
    hd = load_lines(HAND)
    towns = json.load(open(TOWNS, encoding="utf-8"))

    hexes = sorted(r for r in pk if r in hd) if a.all else a.hexes
    if not hexes:
        raise SystemExit("give hex names or --all")

    # The per-line figure the handover quotes, for reference. Reporting it
    # alongside makes explicit that the route-based number is a DIFFERENT
    # measure, not a correction of the same one.
    print("per-line turns, unrouted:  pak %d   hand %d"
          % (per_line_turns(pk, hexes), per_line_turns(hd, hexes)))
    print()
    print("%-20s %6s %8s %8s %8s %8s" %
          ("hex", "pairs", "routed", "turns p", "turns h", "excess"))
    print("-" * 64)
    agg_p, agg_h = [], []
    for R in hexes:
        gp = graph([to_px(R, l) for l in pk[R]])
        gh = graph([to_px(R, l) for l in hd[R]])
        if not gp or not gh:
            print("%-20s   no network" % R)
            continue
        (nodep, adjp, ptp), (nodeh, adjh, pth) = gp, gh

        ox, oy = grid.hex_origin(R)
        s = grid.W / MAP_PX
        # towns.json x,y are ALREADY world units (same frame as Roads.json
        # after the TOWN_FRAME shift), so no mercator factor here. Applying
        # one collapsed all 42 DeadLands towns onto a single node and every
        # "route" became a zero-length path reporting 0 turns.
        tl = []
        for v in towns.values():
            if v.get("region") != R:
                continue
            wx, wy = v["x"] + TOWN_FRAME[0], v["y"] + TOWN_FRAME[1]
            tl.append((v["name"], (wx - ox) / s, (oy - wy) / s))
        if len(tl) < 2:
            print("%-20s   <2 towns" % R)
            continue
        Q = np.array([[t[1], t[2]] for t in tl])
        dp, ip = cKDTree(nodep).query(Q)
        dh, ih = cKDTree(nodeh).query(Q)
        sp = {t[0]: int(i) for t, d, i in zip(tl, dp, ip) if d <= TOWN_SNAP_PX}
        sh = {t[0]: int(i) for t, d, i in zip(tl, dh, ih) if d <= TOWN_SNAP_PX}
        names = sorted(set(sp) & set(sh))
        if len(names) < 2:
            print("%-20s   <2 shared towns" % R)
            continue

        npair = routed = 0
        tp, th = [], []
        for x in range(len(names)):
            for y in range(x + 1, min(x + 1 + a.max_pairs, len(names))):
                if npair >= a.max_pairs:
                    break
                npair += 1
                qa = path_from(adjp, sp[names[x]], sp[names[y]])
                qb = path_from(adjh, sh[names[x]], sh[names[y]])
                if qa is None or qb is None:
                    continue
                routed += 1
                tp.append(len(turns_of(densify(ptp, qa))))
                th.append(len(turns_of(densify(pth, qb))))
        if not tp:
            print("%-20s   no route pairs" % R)
            continue
        agg_p += tp
        agg_h += th
        mp, mh = float(np.mean(tp)), float(np.mean(th))
        print("%-20s %6d %8d %8.1f %8.1f %7.0f%%" %
              (R, npair, routed, mp, mh, 100.0 * (mp - mh) / max(0.5, mh)))
        sys.stdout.flush()

    if agg_p and agg_h:
        mp, mh = float(np.mean(agg_p)), float(np.mean(agg_h))
        print("-" * 64)
        print("%-20s %6d %8d %8.1f %8.1f %7.0f%%" %
              ("ALL ROUTES", len(agg_p), len(agg_p), mp, mh,
               100.0 * (mp - mh) / max(0.5, mh)))


if __name__ == "__main__":
    main()
