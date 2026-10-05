#!/usr/bin/env python3
"""snap_coincident.py -- merge vertices that are the same point on the ground.

Two road ends 19 mm apart are the same junction to any purpose: the map renders
at about a metre per pixel, and a routing engine that treats them as distinct
will happily route 900 m around them.  This is a final, deliberately dumb pass
that unions any vertices closer than --tol, and moves them onto a common point.

It runs AFTER welding and splitting, and it ignores every heuristic those
tools use -- no angle test, no conflict rule, no component check -- because
at this scale the only question is whether the two coordinates are the same
piece of ground.  A tolerance of 20 mm is still 1/50th of a rendered pixel.
"""
import argparse
import json
import math
import os
import sys
from collections import defaultdict

import numpy as np
from scipy.spatial import cKDTree

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import graphkit as gk


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--in', dest='src', required=True)
    ap.add_argument('--out', dest='dst', required=True)
    ap.add_argument('--tol', type=float, default=0.02,
                    help='world units; 0.02 = 20 mm')
    a = ap.parse_args()

    net = gk.load(a.src)
    lines = [(r, [list(p) for p in w]) for r, w in net.lines]
    pts, where = [], []
    for li, (r, w) in enumerate(lines):
        for k, p in enumerate(w):
            pts.append(p)
            where.append((li, k))
    P = np.array(pts, dtype=float)
    tree = cKDTree(P)
    pairs = tree.query_pairs(a.tol, output_type='ndarray')
    if len(pairs) == 0:
        print('no vertex pairs within %g units' % a.tol)
    # union-find over the pairs
    par = list(range(len(P)))

    def find(x):
        while par[x] != x:
            par[x] = par[par[x]]
            x = par[x]
        return x

    for i, j in pairs:
        ri, rj = find(int(i)), find(int(j))
        if ri != rj:
            par[ri] = rj
    groups = defaultdict(list)
    for i in range(len(P)):
        groups[find(i)].append(i)
    moved = 0
    for root, members in groups.items():
        if len(members) < 2:
            continue
        mx = sum(P[m][0] for m in members) / len(members)
        my = sum(P[m][1] for m in members) / len(members)
        for m in members:
            li, k = where[m]
            lines[li][1][k] = [mx, my]
            moved += 1
    print('snapped %d vertices into %d coincident groups (tol %g u = %g mm)'
          % (moved, sum(1 for g in groups.values() if len(g) > 1), a.tol, a.tol * 1000))

    new = gk.Network(lines, path=a.dst)
    vc, vl = new.vertex_components()
    print('  -> %d vertex comps, largest %d (%.1f%%)'
          % (vc, vl, 100.0 * vl / sum(len(w) for _, w in lines)))

    data = json.load(open(a.src))
    out = []
    fi = 0
    for f in data['features']:
        f = json.loads(json.dumps(f))
        g = f['geometry']
        multi = g['type'] == 'MultiLineString'
        cs = g['coordinates'] if multi else [g['coordinates']]
        newcs = []
        for _ in cs:
            r, w = lines[fi]
            fi += 1
            newcs.append([list(gk.w2m(x, y)) for x, y in w])
        f['geometry']['coordinates'] = newcs if multi else newcs[0]
        out.append(f)
    assert fi == len(lines), 'feature/line mismatch %d vs %d' % (fi, len(lines))
    data['features'] = out
    json.dump(data, open(a.dst, 'w'))
    print('  wrote %s (%d features)' % (a.dst, len(out)))


if __name__ == '__main__':
    main()
