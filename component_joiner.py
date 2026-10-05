#!/usr/bin/env python3
"""component_joiner.py -- connect the network, paying once per component.

Why
---
The export expresses roads as overlapping ribbons with no shared vertices, so
endpoint-chaining joins nothing and the network arrives as 327 fragments.  The
obvious repair is to join every near intersection -- but that pays per junction
(5,691 of them) and draws a long line each time: 879 segments over 150 m to
reach the same connectivity the live build reaches with 27.

Joining every *intersection* is the wrong granularity.  A fragment only needs
ONE link to become routable; the rest of its ends are already connected to
everything behind them.  So this picks, per component, the single shortest link
to any other component, and applies those.  One draw per fragment, not one per
junction, so the geometry cost drops by an order of magnitude for the same
connectivity.

Each round is decided against the geometry as it stands and applied at once, so
no vertex index is ever stale.
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
import join_roads as jr


def build(lines):
    par = {}

    def find(x):
        par.setdefault(x, x)
        r = x
        while par[r] != r:
            r = par[r]
        while par[x] != r:
            par[x], x = r, par[x]
        return r

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            par[ra] = rb
    for li, (r, w) in enumerate(lines):
        for i in range(len(w) - 1):
            union(('v', li, i), ('v', li, i + 1))
        for p in w:
            union(('v', li, 0), ('p', round(p[0], 6), round(p[1], 6)))
            break
        for p in w:
            union(('v', li, len(w) - 1), ('p', round(p[0], 6), round(p[1], 6)))
            break
    return find


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--in', dest='src', required=True)
    ap.add_argument('--out', dest='dst', required=True)
    ap.add_argument('--max-link', type=float, default=3.0, help='world units; longest link to draw')
    ap.add_argument('--rounds', type=int, default=12)
    a = ap.parse_args()

    net = gk.load(a.src)
    lines = [(r, [list(p) for p in w]) for r, w in net.lines]
    J = jr.Joiner(lines, 0.30, 20.0, 160.0, 160.0)
    def comps():
        f2 = jr.Joiner(lines, 0.30, 20.0, 160.0, 160.0).build_components()
        cnt = Counter()
        for li, (r, w) in enumerate(lines):
            for vi in range(len(w)):
                cnt[f2(('v', li, vi))] += 1
        return cnt
    c0 = comps()
    print('start: %d lines, %d components' % (len(lines), len(c0)))

    total_links = 0
    for rnd in range(a.rounds):
        find = jr.Joiner(lines, 0.30, 20.0, 160.0, 160.0).build_components()
        # per-component member vertex lists
        members = defaultdict(list)
        for li, (r, w) in enumerate(lines):
            for vi in range(len(w)):
                members[find(('v', li, vi))].append((li, vi, tuple(w[vi])))
        eps = []
        for c, ms in members.items():
            eps += [(li, vi, np.array(p), c) for li, vi, p in ms]
        E = np.array([e[2] for e in eps])
        comps_of = np.array([hash(e[3]) for e in eps])
        tree = cKDTree(E)
        K = min(48, len(E))
        d, idx = tree.query(E, k=K)
        best = {}
        for i in range(len(eps)):
            for c in range(1, K):
                if d[i][c] > a.max_link:
                    break
                j = int(idx[i][c])
                if comps_of[j] == comps_of[i]:
                    continue
                key = comps_of[i]
                if key not in best or d[i][c] < best[key][0]:
                    best[key] = (d[i][c], i, j)
        if not best:
            print('round %d: no component link within %g units' % (rnd, a.max_link))
            break
        # one link per component, and never two components linked twice
        used_c = set()
        moves = []
        for c in sorted(best, key=lambda k: best[k][0]):
            dist, i, j = best[c]
            if c in used_c or comps_of[j] in used_c:
                continue
            used_c.add(c); used_c.add(comps_of[j])
            moves.append((eps[i], eps[j], float(dist)))
        for (li, vi, P, _c), (lj, vj, Q, _c2), dist in moves:
            w = lines[li][1]
            tgt = (float(Q[0]), float(Q[1]))
            w[0 if vi == 0 else len(w) - 1] = [tgt[0], tgt[1]]
            w2 = lines[lj][1]
            w2[0 if vj == 0 else len(w2) - 1] = [tgt[0], tgt[1]]
        total_links += len(moves)
        nvc, nvl = gk.Network(lines, path=a.dst).vertex_components()
        print('round %d: %3d links (max %.0f m) -> %3d components, largest %.1f%%'
              % (rnd, len(moves), max(m[2] for m in moves) * 100,
                 nvc, 100.0 * nvl / sum(len(w) for _, w in lines)))

    nvc, nvl = gk.Network(lines, path=a.dst).vertex_components()
    print('final: %d components, largest %.1f%%, %d links drawn'
          % (nvc, 100.0 * nvl / sum(len(w) for _, w in lines), total_links))
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
    data['features'] = out
    json.dump(data, open(a.dst, 'w'))
    print('wrote %s' % a.dst)


if __name__ == '__main__':
    main()
