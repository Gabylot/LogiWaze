"""Contracted routable graph: pixels -> nodes at junctions/endpoints/branch
   points, with edges running between them.  This is the same decomposition the
   extractor already performs for its own edge walking, so the graph is
   routable and small (thousands of nodes, not millions of pixels).
"""
import sys
sys.path.insert(0, '/var/www/LogiWaze')
import numpy as np
from scipy import ndimage as ndi
import heapq


def contracted_graph(sk):
    """sk: bool skeleton.  -> (pts, adj) with pts in pixel coords."""
    ys, xs = np.nonzero(sk)
    if len(ys) == 0:
        return None
    on = set(zip(ys.tolist(), xs.tolist()))
    H, W = sk.shape

    def nbrs(y, x):
        out = []
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if dy == 0 and dx == 0:
                    continue
                p = (y + dy, x + dx)
                if p in on:
                    out.append(p)
        return out

    deg = {}
    for (y, x) in on:
        deg[(y, x)] = len(nbrs(y, x))
    seeds = [p for p in on if deg[p] != 2]
    if not seeds:
        seeds = list(on)
    # cluster neighbouring seeds
    parent = {p: p for p in on}

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra
    # Only SEED pixels (degree != 2) become nodes.  Runs of degree-2 pixels
    # between two seeds are the EDGES.  Merging every adjacent pixel instead
    # would fuse the whole network into one node, which is what an earlier
    # version did.
    seed_id = {}
    pts = []
    for p in seeds:
        seed_id[p] = len(pts)
        pts.append((float(p[1]), float(p[0])))
    # an isolated closed loop with no seed: give it one node
    if not seed_id:
        y, x = next(iter(on))
        seed_id[(y, x)] = 0
        pts = [(float(x), float(y))]
    adj = [dict() for _ in pts]

    def node_near(p):
        return seed_id[p] if p in seed_id else None

    visited_edge = set()
    for p in seeds:
        u = seed_id[p]
        for q in nbrs(*p):
            if (p, q) in visited_edge:
                continue
            # walk the degree-2 run from p to the next seed
            prev, cur = p, q
            length = np.hypot(q[1] - p[1], q[0] - p[0])
            visited_edge.add((p, q))
            guard = 0
            while cur not in seed_id and deg[cur] == 2 and guard < 100000:
                guard += 1
                nxt = [z for z in nbrs(*cur) if z != prev]
                if not nxt:
                    break
                nn = nxt[0]
                length += np.hypot(nn[1] - cur[1], nn[0] - cur[0])
                visited_edge.add((cur, nn))
                prev, cur = cur, nn
            if cur in seed_id and cur != p:
                v = seed_id[cur]
                if v != u:
                    if v not in adj[u] or adj[u][v] > length:
                        adj[u][v] = length
                        adj[v][u] = length
    return np.array(pts), [sorted(d.items()) for d in adj]


def dijkstra(adj, src):
    dist = {src: 0.0}
    pq = [(0.0, src)]
    while pq:
        d, u = heapq.heappop(pq)
        if d > dist.get(u, 1e18) + 1e-12:
            continue
        for v, w in adj[u]:
            nd = d + w
            if nd < dist.get(v, 1e18) - 1e-12:
                dist[v] = nd
                heapq.heappush(pq, (nd, v))
    return dist
