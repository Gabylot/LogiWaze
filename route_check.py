"""Route-level check with REAL locations, comparing paths and turns.

Everything measured so far compared geometry.  What LogiWaze actually does is
route a vehicle between two real places and report turn instructions, so that
is what this tests:

  1. pick real town pairs (the ones a player would route between)
  2. compute the actual path through the EXTRACTED network
  3. compute the actual path through the HAND-DRAWN network
  4. measure
       path overlap   - do the two routes run along the same road?
       turn agreement - same number of turns, and same turn direction?

Turns are derived the way leaflet-routing-machine derives them: from the
bearing change between consecutive path vertices, bucketed into
straight / bear / turn / sharp / u-turn.
"""
import sys, os, json, math, argparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from PIL import Image
from scipy.spatial import cKDTree
import extract_routes as er
import grid
import netdiff
from routegraph import contracted_graph, dijkstra

HERE = os.path.dirname(os.path.abspath(__file__))
TOWN_FRAME = (128.25, -128.25)
TOWN_SNAP_PX = 600.0

# leaflet-routing-machine's angle buckets, in degrees
BUCKETS = [(0, 20, 'straight'), (20, 50, 'bear'), (50, 125, 'turn'),
           (125, 170, 'sharp'), (170, 181, 'u-turn')]


def turn_bearing(a, b, c):
    """Signed turn in degrees at b, given the previous point a and next c."""
    b1 = math.degrees(math.atan2(b[0] - a[0], b[1] - a[1]))
    b2 = math.degrees(math.atan2(c[0] - b[0], c[1] - b[1]))
    d = (b2 - b1 + 180) % 360 - 180
    return d


def turn_label(deg):
    d = abs(deg)
    for lo, hi, name in BUCKETS:
        if lo <= d < hi:
            side = 'left' if deg < 0 else 'right'
            return 'straight' if name == 'straight' else name + '-' + side
    return 'straight'


def turns_of(path, tol_deg=12.0, min_leg_px=10.0):
    """Turn instructions for a densified polyline.

    The walk keeps an ANCHOR and advances a probe, comparing the bearing from
    the anchor to the probe against the anchor's initial bearing.  When that
    deviation exceeds tol_deg the corner is between them, so the anchor moves
    there and the walk restarts.

    An earlier version advanced the probe while segments were SHORTER than a
    run length.  That is inverted for a densified path, where every segment is
    a couple of pixels: the probe ran to the end of the route and reported zero
    turns for every pair, which looked like 0% turn agreement rather than the
    bug it was.
    """
    if len(path) < 3:
        return []
    pts = [(float(p[0]), float(p[1])) for p in path]
    out = []
    i = 0
    n = len(pts)
    while i < n - 1:
        base = math.atan2(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1])
        j = i + 1
        found = None
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
        out.append((found[0], found[1]))
        i = j
    return out


def path_from_dist(pts, adj, src, dst):
    """Reconstruct the node sequence of the shortest path."""
    dist = {src: 0.0}
    prev = {}
    pq = [(0.0, src)]
    seen = set()
    while pq:
        import heapq
        d, u = heapq.heappop(pq)
        if u in seen:
            continue
        seen.add(u)
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('hexes', nargs='+')
    ap.add_argument('--max-pairs', type=int, default=40)
    a = ap.parse_args()
    grid.load_offsets()
    lz = json.load(open(os.path.join(HERE, 'Roads.json')))
    towns = json.load(open(os.path.join(HERE, 'towns.json')))
    byreg = {}
    for f in lz['features']:
        byreg.setdefault(f['properties']['region'], []).append(f)
    print('%-20s %6s %6s %9s %9s %9s %8s' %
          ('hex', 'pairs', 'routed', 'path ovl', 'avg turns', 'turn match', 'dist e/h'))
    print('-' * 74)
    tot = dict(pairs=0, routed=0, ovl=0.0, turn_e=0, turn_h=0, match=0.0, ov=0.0, oh=0.0)
    for R in a.hexes:
        if R not in byreg:
            continue
        base = np.array(Image.open(er.find_png(R)).convert('RGB'))
        H, W = base.shape[:2]
        tx, ty = grid.origin_from_table(R)
        comb, _, _ = er.road_skeleton(base)
        ge = contracted_graph(comb)
        gh = contracted_graph(netdiff.rasterise(byreg[R], H, W, tx, ty))
        if not ge or not gh:
            continue
        (pe, ae), (ph, ah) = ge, gh
        tl = []
        for v in towns.values():
            if v.get('region') != R:
                continue
            wx, wy = v['x'] + TOWN_FRAME[0], v['y'] + TOWN_FRAME[1]
            tl.append((v['name'], (wx - tx) / er.S, (ty - wy) / er.S))
        if len(tl) < 2:
            continue
        Q = np.array([[t[1], t[2]] for t in tl])
        de, ie = cKDTree(pe).query(Q)
        dh, ih = cKDTree(ph).query(Q)
        se = {t[0]: int(i) for t, d, i in zip(tl, de, ie) if d <= TOWN_SNAP_PX}
        sh = {t[0]: int(i) for t, d, i in zip(tl, dh, ih) if d <= TOWN_SNAP_PX}
        names = sorted(set(se) & set(sh))
        npair = routed = 0
        ovl = []; ov = oh = 0.0; mt = []; nte = [0, 0]; nth = [0, 0]
        for x in range(len(names)):
            for y in range(x + 1, len(names)):
                if npair >= a.max_pairs:
                    break
                A, B = names[x], names[y]
                npair += 1
                pa = path_from_dist(pe, ae, se[A], se[B])
                pb = path_from_dist(ph, ah, sh[A], sh[B])
                if pa is None or pb is None:
                    continue
                routed += 1
                ra = densify(pe, pa)
                rb = densify(ph, pb)
                ovl.append(cKDTree(ph).query(np.array(ra))[0].mean())
                ov += sum(math.dist(rb[k], rb[k + 1]) for k in range(len(rb) - 1))
                oh += sum(math.dist(ra[k], ra[k + 1]) for k in range(len(ra) - 1))
                ta, tb = turns_of(ra), turns_of(rb)
                if ta or tb:
                    mt.append(100.0 * min(len(ta), len(tb)) /
                              max(1, max(len(ta), len(tb))))
                    nte[0] += len(ta); nth[0] += len(tb); nte[1] += 1
        if not ovl:
            continue
        avg_e = nte[0] / max(1, nte[1]); avg_h = nth[0] / max(1, nte[1])
        print('%-20s %6d %6d %8.1f%% %9.1f %9.1f%% %7.3f' %
              (R, npair, routed, np.mean(ovl), avg_e,
               np.mean(mt) if mt else 0.0, oh / max(1e-9, ov)))
        tot['pairs'] += npair; tot['routed'] += routed
        tot['ov'] += ov; tot['oh'] += oh
        sys.stdout.flush()
    print('-' * 74)
    print('total pairs %d, routed in BOTH %d' % (tot['pairs'], tot['routed']))


if __name__ == '__main__':
    main()
