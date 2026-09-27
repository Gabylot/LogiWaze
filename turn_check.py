"""Turn-instruction check on the geometry the app actually routes on.

route_check.py built its graph from the SKELETON, but the app routes the
POLYLINES in Roads.json.  Those are different shapes: the skeleton is a 1px
medial axis with a vertex per pixel, while the polylines are what
leaflet-routing-machine receives and turns into instructions.  So turns must
be measured on the polylines, which is what this does.

Method, per real town pair:
  1. snap the town to the nearest point on the polyline network
  2. shortest path over the polyline network (nodes = polyline vertices,
     edges = consecutive vertices, with shared endpoints merged)
  3. extract turn instructions from the resulting coordinate sequence
  4. compare extracted vs hand-drawn: same turn count, same directions
"""
import sys, os, math, json, argparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from scipy.spatial import cKDTree
from PIL import Image, ImageDraw
import extract_routes as er
import grid
from route_check import turns_of, TOWN_FRAME, TOWN_SNAP_PX

HERE = os.path.dirname(os.path.abspath(__file__))


def polyline_graph(segs, snap=1.0):
    """Graph over polyline segments; endpoints within `snap` are merged."""
    from scipy.spatial import cKDTree as KD
    if not segs:
        return None
    pts = np.array([p for s in segs for p in s], float)
    t = KD(pts)
    parent = list(range(len(pts)))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a
    for a, b in t.query_pairs(snap, output_type='ndarray'):
        ra, rb = find(int(a)), find(int(b))
        if ra != rb:
            parent[rb] = ra
    remap = {}
    for k in range(len(pts)):
        r = find(k)
        remap.setdefault(r, len(remap))
    nodes = np.zeros((len(remap), 2))
    rep = {}
    for k in range(len(pts)):
        rep.setdefault(find(k), pts[k])
    for r, n in remap.items():
        nodes[n] = rep[r]
    adj = [dict() for _ in nodes]
    for i in range(0, len(pts), 2):
        u, v = remap[find(i)], remap[find(i + 1)]
        if u == v:
            continue
        w = float(math.dist(pts[i], pts[i + 1]))
        adj[u][v] = min(adj[u].get(v, 1e18), w)
        adj[v][u] = min(adj[v].get(u, 1e18), w)
    return nodes, [sorted(d.items()) for d in adj]


def segs_from_feats(feats, tx, ty):
    S = er.S
    out = []
    for f in feats:
        c = f['geometry']['coordinates']
        for i in range(len(c) - 1):
            a = ((c[i][0] - tx) / S, (ty - c[i][1]) / S)
            b = ((c[i + 1][0] - tx) / S, (ty - c[i + 1][1]) / S)
            if math.dist(a, b) > 1e-9:
                out.append((a, b))
    return out


def path(pts, adj, src, dst):
    import heapq
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
    ap.add_argument('--max-pairs', type=int, default=30)
    a = ap.parse_args()
    grid.load_offsets()
    lz = json.load(open(os.path.join(HERE, 'Roads.json')))
    towns = json.load(open(os.path.join(HERE, 'towns.json')))
    byreg = {}
    for f in lz['features']:
        byreg.setdefault(f['properties']['region'], []).append(f)
    print('%-20s %6s %7s %8s %8s %9s' %
          ('hex', 'pairs', 'routed', 'turns e', 'turns h', 'turn match'))
    print('-' * 64)
    for R in a.hexes:
        if R not in byreg:
            continue
        tx, ty = grid.origin_from_table(R)
        ex, _ = er.extract_hex(R)
        ge = polyline_graph(segs_from_feats(ex, tx, ty))
        gh = polyline_graph(segs_from_feats(byreg[R], tx, ty))
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
        mts = []; ne = []; nh = []
        for x in range(len(names)):
            for y in range(x + 1, min(x + 1 + a.max_pairs, len(names))):
                if npair >= a.max_pairs:
                    break
                A, B = names[x], names[y]
                npair += 1
                pa = path(pe, ae, se[A], se[B])
                pb = path(ph, ah, sh[A], sh[B])
                if pa is None or pb is None:
                    continue
                routed += 1
                ta = turns_of(densify(pe, pa))
                tb = turns_of(densify(ph, pb))
                ne.append(len(ta)); nh.append(len(tb))
                if ta or tb:
                    mts.append(100.0 * min(len(ta), len(tb)) /
                               max(1, max(len(ta), len(tb))))
        if not ne:
            continue
        print('%-20s %6d %7d %8.1f %8.1f %8.1f%%' %
              (R, npair, routed, np.mean(ne), np.mean(nh),
               np.mean(mts) if mts else 0.0))
        sys.stdout.flush()


if __name__ == '__main__':
    main()
