#!/usr/bin/env python3
"""graphkit.py -- shared graph model for the road network.

Why this exists
---------------
A road network is a graph whose nodes are line endpoints and whose edges are
the line segments between them.  Routing joins by *shared endpoint*, so two
pieces of road that stop a centimetre apart are as disconnected as two pieces
a kilometre apart.  Every measurement here depends on getting that model
exactly right, and getting it wrong produced three false conclusions during
development:

  * keying union-find nodes by line index but querying by endpoint index gave
    nonsense component counts;
  * counting a road that ENDS MID-LINE against another road as a connection
    made the network look far healthier than it is;
  * treating coincident vertices as "near misses" reported 17,812 phantom
    gaps in the hand-traced network, which is actually well connected.

So the definitions live here, once, with tests.

Definitions
-----------
node        a rounded endpoint coordinate, 6 decimal places in world units
            = 0.1 mm: fine enough to be lossless, coarse enough to join
            values written by different producers.

connection  two endpoints are connected if they are ends of the same line, OR
            they share a node.  Nothing else.  In particular a road ending in
            the middle of another road is NOT a connection, because no engine
            that joins on endpoints will find it.

near-miss   a pair of endpoints, in DIFFERENT components, closer than a
            threshold.  The "different components" clause separates a real
            gap from two coincident duplicates.
"""
import json
import math
from collections import defaultdict, Counter

import numpy as np
from scipy.spatial import cKDTree

# Endpoints per block in near_misses()'s k-nearest query.  2048 keeps the
# (chunk, k) result at a few MB, so peak memory tracks the chunk and not the
# whole network.
CHUNK = 2048

HALF, SCALE = 128.0, 128.0 / 20037500.0
COORD_DP = 6          # decimals kept when keying a node


def m2w(p):
    """EPSG:3857 -> LogiWaze world units, the frame the routing engine uses."""
    return (p[0] * SCALE + HALF, p[1] * SCALE - HALF)


def w2m(x, y):
    """World units -> EPSG:3857, the inverse used when writing data back."""
    return ((x - HALF) / SCALE, (y + HALF) / SCALE)


def node_of(x, y):
    """Quantise a world coordinate to a graph node key."""
    return (round(x, COORD_DP), round(y, COORD_DP))


def load(path):
    """Read a road_source.geojson into a Network.  Handles LineString and MultiLineString."""
    lines = []
    for f in json.load(open(path))['features']:
        region = f['properties'].get('region', '?')
        g = f.get('geometry')
        if not g:
            continue
        cs = g['coordinates'] if g['type'] == 'MultiLineString' else [g['coordinates']]
        for ln in cs:
            w = [m2w(p) for p in ln]
            if len(w) >= 2:
                lines.append((region, w))
    return Network(lines, path=path)


class DSU:
    def __init__(self):
        self.p = {}

    def find(self, x):
        self.p.setdefault(x, x)
        root = x
        while self.p[root] != root:
            root = self.p[root]
        while self.p[x] != root:          # iterative path compression
            self.p[x], x = root, self.p[x]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[ra] = rb
            return True
        return False


class Network:
    """A road network as a graph, plus the measurements that matter.

    ends: one entry per line endpoint --
          (node, region, line_index, 'S'|'E', outward_direction)
    The outward direction is the direction pointing from the line's body out
    through that endpoint.  For a pass-through join (one line ends where
    another starts) two straight roads have parallel outward directions, so
    the angle between them is ~0.
    """

    def __init__(self, lines, path=None):
        self.path = path
        self.lines = lines
        self.ends = []
        for li, (region, w) in enumerate(lines):
            a = np.array(w[0], dtype=float)
            b = np.array(w[-1], dtype=float)
            self.ends.append((node_of(*w[0]), region, li, 'S', a - np.array(w[1], dtype=float)))
            self.ends.append((node_of(*w[-1]), region, li, 'E', np.array(w[-2], dtype=float) - b))
        self._build()

    def _build(self):
        self.dsu = DSU()
        by_line = defaultdict(list)
        for i, e in enumerate(self.ends):
            self.dsu.union(('e', i), ('n',) + e[0])
            by_line[e[2]].append(('e', i))
        for grp in by_line.values():          # ends of one line are connected
            for k in grp[1:]:
                self.dsu.union(grp[0], k)
        self.comp = [self.dsu.find(('e', i)) for i in range(len(self.ends))]
        sizes = Counter(self.comp)
        self.n_components = len(sizes)
        self.largest_component = max(sizes.values()) if sizes else 0
        self.segs = []
        for region, w in self.lines:
            for i in range(len(w) - 1):
                a = np.array(w[i], dtype=float)
                b = np.array(w[i + 1], dtype=float)
                self.segs.append((a, b, math.hypot(*(b - a)), region))
        self.total_length = sum(s[2] for s in self.segs)
        self._pts = np.array([(e[0][0], e[0][1]) for e in self.ends])

    def vertex_components(self):
        """Connectivity using EVERY vertex as a node, not just endpoints.

        leaflet-routing-machine builds its graph from the waypoints of each
        polyline, so two lines that share ANY vertex are connected -- including
        a road that ends in the middle of another road.  That is the model the
        browser actually runs, and it is much more permissive than the
        endpoint model.

        Both are reported because they disagree sharply on this data, and the
        gap between them is itself diagnostic: a large difference means lines
        are relying on shared interior vertices to connect, which breaks the
        moment a neighbour's vertex is not exactly coincident.
        """
        dsu = DSU()
        for li, (region, w) in enumerate(self.lines):
            for vi, p in enumerate(w):
                dsu.union(('v', li, vi), ('n',) + node_of(*p))
                if vi:                      # consecutive vertices are one road
                    dsu.union(('v', li, vi), ('v', li, vi - 1))
        keys = [('v', li, vi) for li, (r, w) in enumerate(self.lines)
                for vi in range(len(w))]
        sizes = Counter(dsu.find(k) for k in keys)
        # keep them, so callers can ask which component a given vertex is in.
        # An earlier version threw the DSU away, and querying the ENDPOINT
        # union-find with vertex keys silently minted a fresh singleton per
        # lookup, which reported every hex as thousands of separate islands.
        self._vdsu, self._vkeys = dsu, keys
        return len(sizes), (max(sizes.values()) if sizes else 0)

    def vertex_comp(self, line_idx, vertex_idx):
        """Component id of a specific vertex under the vertex model."""
        if getattr(self, '_vdsu', None) is None:
            self.vertex_components()
        return self._vdsu.find(('v', line_idx, vertex_idx))

    def comp_at(self, x, y):
        """(component, distance, endpoint_index) of the endpoint nearest a world point."""
        d, i = cKDTree(self._pts).query((x, y))
        return self.comp[int(i)], float(d), int(i)

    def nearest_seg(self, x, y):
        """Distance to the nearest road SEGMENT and the component of that segment.

        Projecting onto segments matters: a town mid-road can sit hundreds of
        metres from the nearest endpoint and be perfectly routable.
        """
        A = np.array([s[0] for s in self.segs])
        B = np.array([s[1] for s in self.segs])
        d = B - A
        L2 = (d * d).sum(1)
        L2[L2 == 0] = 1e-12
        Q = np.array([x, y])
        t = (((Q - A) * d).sum(1) / L2).clip(0, 1)
        proj = A + t[:, None] * d
        dist = np.hypot(*(Q - proj).T)
        i = int(dist.argmin())
        return float(dist[i]), self.segs[i]

    def near_misses(self, max_gap):
        """Endpoint pairs in DIFFERENT components closer than max_gap.

        Each endpoint is offered at most once, and a pair is only reported if
        each endpoint is the other's nearest *cross-component* candidate.

        The "cross-component" qualifier matters.  Querying a plain k=2 nearest
        neighbour lets a coincident duplicate sitting on the same node steal
        the slot, so a genuine 0.1-unit gap next to a joined node pair is never
        offered and mutual matching silently fails.  That is not hypothetical:
        fork/merge junctions put several endpoints on one coordinate.
        """
        n = len(self.ends)
        k = min(16, n)
        tree = cKDTree(self._pts)
        comp = np.array([hash(c) for c in self.comp], dtype=np.int64)
        best = {}
        kk = k
        while kk <= n:
            # Chunked: a full (n, kk) float64 result is ~300 MB at kk=16 for a
            # 37k-endpoint network, and query() allocates it in one block, which
            # fails outright on a small box.  Stepping in blocks keeps the peak
            # proportional to the chunk rather than the whole network.
            for lo in range(0, n, CHUNK):
                hi = min(n, lo + CHUNK)
                d, idx = tree.query(self._pts[lo:hi], k=kk)
                if kk == 1:
                    d = d[:, None]
                    idx = idx[:, None]
                for i in range(lo, hi):
                    if i in best:
                        continue
                    for c in range(1, kk):
                        j = int(idx[i - lo][c])
                        if comp[j] == comp[i] or d[i - lo][c] > max_gap:
                            continue
                        best[i] = (float(d[i - lo][c]), j)
                        break
            if len(best) == n or kk >= n or kk >= 64:
                # kk grows until every endpoint has a cross-component candidate.
                # Capped at 64: past that the query costs kk*log(n) per endpoint
                # and stops paying for itself.  Endpoints with no match in 64
                # neighbours are left alone -- a genuine near miss is a local
                # one, so a miss this far out is not one, and reporting it would
                # mean welding unrelated roads (e.g. a carriageway to a sidewalk).
                break
            kk = min(n, kk * 4)
        out, used = [], set()
        for i, (gap, j) in best.items():
            if i in used or j in used:
                continue
            if best.get(j, (0, None))[1] != i:
                continue
            used.add(i); used.add(j)
            out.append((gap, i, j))
        return out

    def _vertex_counts(self):
        """node -> how many vertices (endpoints AND interior) sit on it.

        Interior vertices must be counted, not just endpoints.  A road's
        endpoint can be coincident with the MIDDLE of another road, and moving
        it detaches that connection.  Counting only endpoints let those welds
        through, which is what made East Narthex -> Transept 19% longer after
        welding.
        """
        if getattr(self, '_vcounts', None) is None:
            c = {}
            for li, (region, w) in enumerate(self.lines):
                for p in w:
                    k = node_of(*p)
                    c[k] = c.get(k, 0) + 1
            self._vcounts = c
        return self._vcounts

    def coincidence(self, i):
        """How many vertices sit on this endpoint's node."""
        return self._vertex_counts().get(self.ends[i][0], 0)

    def coincidence_at(self, node):
        return self._vertex_counts().get(node, 0)

    def join_kind(self, i, j):
        """('pass-through'|'fork/merge', angle_degrees) for a candidate join.

        pass-through : one line ENDS where another STARTS.  Straight-through
                       is ~0 deg; large is a hairpin.
        fork/merge   : both start, or both end.  They are the two arms of a
                       junction, so ~180 deg means the road runs straight
                       through the junction, which is correct.
        """
        wa, da = self.ends[i][3], self.ends[i][4]
        wb, db = self.ends[j][3], self.ends[j][4]
        na, nb = np.hypot(*da), np.hypot(*db)
        if na < 1e-12 or nb < 1e-12:
            return None
        c = float(np.dot(da, db)) / (na * nb)
        ang = math.degrees(math.acos(max(-1.0, min(1.0, c))))
        return ('pass-through' if wa != wb else 'fork/merge', ang)
