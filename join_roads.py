#!/usr/bin/env python3
"""join_roads.py -- make a road network connected by joining ends that nearly meet.

Why
---
Extracted road is drawn as many separate pieces.  Measured on this data the
median gap from a road end to the nearest other road end is 19.6 m, and only
11% of ends have a partner within 2 mm -- so ~89% of the network starts life as
islands.  The linework itself is accurate (99.2% agreement with the hand trace
inside 10 m); what is missing is endpoint *coincidence*, which is the only
thing a router joins on.  Positional agreement and connectivity are
independent properties, and only the second one matters here.

Design
------
Each round finds every candidate join, scores them, and applies the whole
round AT ONCE, then rebuilds from scratch.  Deciding and editing in the same
pass is what produced 180 m phantom diagonals before: vertex indices computed
early in a pass are stale by the time later ones are used.  Recomputing each
round makes that impossible by construction.

A join must earn its place:
  - the pieces are not already connected
  - the angle is one a real junction has: a continuation, or a T
  - an end does not grab an unbounded number of neighbours
"""
import argparse
import json
import math
import os
import sys
from collections import defaultdict, Counter

import numpy as np
from scipy.spatial import cKDTree

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import graphkit as gk


def unit(v):
    n = math.hypot(v[0], v[1])
    return (v[0] / n, v[1] / n) if n > 1e-12 else (0.0, 0.0)


def neg(v):
    return (-v[0], -v[1])


def angle(a, b):
    ua, ub = unit(a), unit(b)
    c = ua[0] * ub[0] + ua[1] * ub[1]
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


def travel_dir(lines, li, end):
    """Direction of travel as you leave the network through this end.

    lines[li] is (region, vertices) -- indexing it directly reads the region
    NAME as if it were a coordinate, which is what this did until it failed
    loudly on a subtraction.
    """
    w = lines[li][1]
    if end == 0:
        return unit((w[1][0] - w[0][0], w[1][1] - w[0][1]))
    return unit((w[-1][0] - w[-2][0], w[-1][1] - w[-2][1]))


class Joiner:
    def __init__(self, lines, max_gap, t_min, t_max, max_turn, kv=60, knn=40,
                 t_max_gap=None, require_heading=True):
        self.lines = lines
        self.max_gap = max_gap
        self.t_min, self.t_max, self.max_turn = t_min, t_max, max_turn
        self.kv, self.knn = kv, knn
        # A T may reach further than an end-to-end join: it attaches to a road
        # that already exists there, so the geometry it adds is a continuation
        # rather than a bridge across open country.
        self.t_max_gap = max_gap if t_max_gap is None else t_max_gap
        self.require_heading = require_heading
        self.vpos, self.vline, self.vidx = [], [], []
        for li, (r, w) in enumerate(lines):
            for k, p in enumerate(w):
                self.vpos.append(p); self.vline.append(li); self.vidx.append(k)
        self.vpos = np.array(self.vpos, dtype=float)
        self.vline = np.array(self.vline); self.vidx = np.array(self.vidx)

    def endpoints(self):
        out = []
        for li, (r, w) in enumerate(self.lines):
            if len(w) < 2:
                continue
            out.append((li, 0, np.array(w[0], dtype=float)))
            out.append((li, 1, np.array(w[-1], dtype=float)))
        return out

    def build_components(self):
        """Union-find over exact shared endpoints -- the graph a router sees.

        Built ONCE per round.  Doing it per endpoint is O(n^2) and made the
        first run appear to hang.
        """
        par = {}

        def find(x):
            par.setdefault(x, x)
            root = x
            while par[root] != root:
                root = par[root]
            while par[x] != root:
                par[x], x = root, par[x]
            return root

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                par[ra] = rb

        for li, (r, w) in enumerate(self.lines):
            for i in range(len(w) - 1):
                union(('v', li, i), ('v', li, i + 1))
        for k in range(len(self.vpos)):
            union(('v', int(self.vline[k]), int(self.vidx[k])),
                  ('c', round(float(self.vpos[k][0]), 6), round(float(self.vpos[k][1]), 6)))
        return find

    def round(self):
        eps = self.endpoints()
        E = np.array([e[2] for e in eps])
        find = self.build_components()
        comp = [find(('c', round(float(e[2][0]), 6), round(float(e[2][1]), 6))) for e in eps]
        # component of every vertex, so a T can be rejected when the host is
        # already in the end's component -- without that guard the joiner
        # re-joins settled pairs every round (5500 joins at 0 m gap) and never
        # converges.
        vcomp = [find(('v', int(self.vline[k]), int(self.vidx[k]))) for k in range(len(self.vpos))]
        vtree = cKDTree(self.vpos)
        etree = cKDTree(E)
        knn = min(self.knn, len(E))
        d, idx = etree.query(E, k=knn)
        kv = min(max(self.kv, 120), len(self.vpos))
        dv, iv = vtree.query(E, k=kv)

        cands = []
        for n, (li, end, P) in enumerate(eps):
            for c in range(1, knn):
                m = int(idx[n][c])
                if d[n][c] > self.max_gap or comp[m] == comp[n]:
                    continue
                lj, ej, _ = eps[m]
                if lj == li:
                    continue
                turn = angle(travel_dir(self.lines, li, end),
                             neg(travel_dir(self.lines, lj, ej)))
                cands.append((-1 if comp[m] != comp[n] else 0, turn, d[n][c],
                              n, m, None))
            td = travel_dir(self.lines, li, end)
            for c in range(kv):
                if dv[n][c] > self.t_max_gap:
                    break
                k = int(iv[n][c])
                lj = int(self.vline[k]); vj = int(self.vidx[k])
                if lj == li:
                    continue
                wj = self.lines[lj][1]
                if vj == 0 or vj == len(wj) - 1:
                    continue
                if vcomp[k] == comp[n]:
                    continue
                # closest point on this host segment
                a, b = wj[vj], wj[vj + 1]
                dv2 = (b[0] - a[0], b[1] - a[1])
                L2 = dv2[0] * dv2[0] + dv2[1] * dv2[1]
                tt = 0.0 if L2 < 1e-18 else max(0.0, min(1.0, (
                    ((P[0] - a[0]) * dv2[0] + (P[1] - a[1]) * dv2[1]) / L2)))
                Q = (a[0] + tt * dv2[0], a[1] + tt * dv2[1])
                gap = math.hypot(P[0] - Q[0], P[1] - Q[1])
                if gap > self.t_max_gap or gap < 1e-9:
                    continue
                # The end must be HEADING toward the junction.  A road that
                # merely happens to pass near another, pointing away from it,
                # is not a T and must not be extended into one.  This is what
                # makes a large radius safe: it tests intent, not proximity.
                if self.require_heading and (Q[0] - P[0]) * td[0] + (Q[1] - P[1]) * td[1] <= 0:
                    continue
                turn = angle(td, dv2)
                cands.append((0, turn, gap, n, None, (lj, vj)))

        cands.sort()
        used_e, used_seg, moves, stats = set(), set(), [], Counter()
        for merges, turn, dist, n, m, interior in cands:
            if n in used_e:
                stats['end-taken'] += 1
                continue
            if interior is not None:
                if not (self.t_min <= turn <= self.t_max):
                    stats['bad-angle'] += 1
                    continue
                if interior in used_seg:
                    stats['seg-taken'] += 1
                    continue
                used_seg.add(interior); used_e.add(n)
                moves.append(('seg', interior, n, dist))
            else:
                if m in used_e:
                    stats['end-taken'] += 1
                    continue
                if turn > self.max_turn:
                    stats['doubles-back'] += 1
                    continue
                used_e.add(n); used_e.add(m)
                moves.append(('end', (n, m), None, dist))
        return moves, stats


def apply_moves(lines, moves, eps):
    """Apply a whole round of joins at once.

    Every decision was made against the geometry as it stood at the start of
    the round, so no index used here can have been invalidated by an earlier
    edit within the same round.
    """
    for kind, target, src, dist in moves:
        if kind == 'end':
            n, m = target
            li, ei, Pi = eps[n]
            lj, ej, Pj = eps[m]
            mid = ((Pi[0] + Pj[0]) / 2.0, (Pi[1] + Pj[1]) / 2.0)
            w = lines[li][1]
            w[0 if ei == 0 else len(w) - 1] = [mid[0], mid[1]]
            w = lines[lj][1]
            w[0 if ej == 0 else len(w) - 1] = [mid[0], mid[1]]
        else:
            # A T junction: the moving end has to ARRIVE at the host.  Writing
            # the end back to where it already was (a no-op) and putting the
            # projection into the host left the two 20 m apart, so every T
            # join was decoration -- 5000 of them a round while the component
            # count sat still at 198.
            (lj, vj), n = target, src
            li, ei, Pi = eps[n]
            wj = lines[lj][1]
            a, b = wj[vj], wj[vj + 1]
            d = (b[0] - a[0], b[1] - a[1])
            L2 = d[0] * d[0] + d[1] * d[1]
            t = 0.0 if L2 < 1e-18 else max(0.0, min(1.0, (
                (Pi[0] - a[0]) * d[0] + (Pi[1] - a[1]) * d[1]) / L2))
            Q = (a[0] + t * d[0], a[1] + t * d[1])
            w = lines[li][1]
            w[0 if ei == 0 else len(w) - 1] = [Q[0], Q[1]]
            wj.insert(vj + 1, [Q[0], Q[1]])
    return lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--in', dest='src', required=True)
    ap.add_argument('--out', dest='dst', required=True)
    ap.add_argument('--max-gap', type=float, default=0.30, help='world units; 0.30 = 30 m')
    ap.add_argument('--no-heading', action='store_true',
                    help='drop the heading-toward guard (overlapping ribbons may not point at the junction)')
    ap.add_argument('--t-max-gap', type=float, default=1.0,
                    help='world units; how far a T may reach (0.10 = 100 m)')
    ap.add_argument('--max-turn', type=float, default=160.0,
                    help='deg; past this a join doubles back rather than continuing')
    ap.add_argument('--t-min', type=float, default=20.0,
                    help='deg; a T meets its host across the road, never along it')
    ap.add_argument('--t-max', type=float, default=160.0)
    ap.add_argument('--rounds', type=int, default=10)
    ap.add_argument('--report')
    a = ap.parse_args()

    net = gk.load(a.src)
    lines = [(r, [list(p) for p in w]) for r, w in net.lines]
    nvert = sum(len(w) for _, w in lines)
    base = gk.Network(lines, path=a.src)
    vc0, vl0 = base.vertex_components()
    print('start: %d lines, %d vertices, %d comps, largest %.1f%%'
          % (len(lines), nvert, vc0, 100.0 * vl0 / nvert))

    report = []
    for rnd in range(a.rounds):
        J = Joiner(lines, a.max_gap, a.t_min, a.t_max, a.max_turn,
                   t_max_gap=a.t_max_gap, require_heading=not a.no_heading)
        moves, stats = J.round()
        if not moves:
            print('round %d: nothing further to join' % rnd)
            break
        apply_moves(lines, moves, J.endpoints())
        new = gk.Network(lines, path=a.dst)
        vc, vl = new.vertex_components()
        gaps = [m[3] for m in moves]
        print('round %d: %4d joins (%d end-end, %d T)  gap med %.0f m max %.0f m'
              '  -> %3d comps, largest %.1f%%'
              % (rnd, len(moves),
                 sum(1 for m in moves if m[0] == 'end'),
                 sum(1 for m in moves if m[0] == 'seg'),
                 np.median(gaps) * 100, max(gaps) * 100, vc,
                 100.0 * vl / max(sum(len(w) for _, w in lines), 1)))
        report.append(dict(round=rnd, joins=len(moves),
                           gap_max=round(max(gaps), 4), comps=vc, largest=vl,
                           stats=dict(stats)))

    net = gk.Network(lines, path=a.dst)
    vc, vl = net.vertex_components()
    print('final: %d comps, largest %.1f%%' % (vc, 100.0 * vl / max(sum(len(w) for _, w in lines), 1)))

    data = json.load(open(a.src))
    out, fi = [], 0
    for f in data['features']:
        f = json.loads(json.dumps(f))
        g = f['geometry']
        multi = g['type'] == 'MultiLineString'
        cs = g['coordinates'] if multi else [g['coordinates']]
        newcs = []
        for _ in cs:
            r, w = lines[fi]; fi += 1
            newcs.append([list(gk.w2m(x, y)) for x, y in w])
        f['geometry']['coordinates'] = newcs if multi else newcs[0]
        out.append(f)
    assert fi == len(lines), 'feature/line mismatch %d vs %d' % (fi, len(lines))
    data['features'] = out
    json.dump(data, open(a.dst, 'w'))
    print('wrote %s (%d features, %d vertices)'
          % (a.dst, len(out), sum(len(w) for _, w in lines)))
    if a.report:
        json.dump(report, open(a.report, 'w'), indent=1)


if __name__ == '__main__':
    main()
