#!/usr/bin/env python3
"""bulk_routes.py -- route many town pairs and compare two networks.

Used as the pre-deployment regression gate.  Reports how many pairs route at
all, and the distribution of distance change, so a change that helps a handful
of routes while breaking others cannot hide behind a few headline numbers.
"""
import argparse
import json
import os
import random
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import graphkit as gk
from route_test import attach_town, dijkstra, vertex_graph, towns, FRAME


def evaluate(path, T, names, sample, seed=7):
    net = gk.load(path)
    adj = vertex_graph(net)
    rnd = random.Random(seed)
    pairs = [(rnd.choice(names), rnd.choice(names)) for _ in range(sample)]
    out = {}
    for a, b in pairs:
        if a not in out:
            out[a] = attach_town(adj, net, T[a], a)
        if b not in out:
            out[b] = attach_town(adj, net, T[b], b)
    ok = 0
    total = 0.0
    miss = []
    per_pair = {}
    for a, b in pairs:
        na = out[a][0]; nb = out[b][0]
        d = 0.0 if na == nb else dijkstra(adj, na).get(nb)
        if d is None:
            miss.append((a, b))
        else:
            ok += 1
            total += d
        per_pair[(a, b)] = d
    return dict(path=path, routed=ok, n=len(pairs), total_km=total * 0.1,
                misses=miss, per_pair=per_pair)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--in', dest='path', required=True)
    ap.add_argument('--compare')
    ap.add_argument('--towns', default='towns.json')
    ap.add_argument('--sample', type=int, default=200)
    a = ap.parse_args()
    T = towns(a.towns)
    # only towns that are actually near a road in EITHER network, otherwise
    # pairs involving a town with no road swamp the result
    names = [n for n, t in T.items()
             if abs(t.get('x', 1e9)) < 1e8 and n.strip()]
    A = evaluate(a.path, T, names, a.sample)
    print('=== %s ===' % os.path.basename(A['path']))
    print('  routed %d/%d pairs (%.0f%%), summed distance %.0f km, %d unroutable'
          % (A['routed'], A['n'], 100.0 * A['routed'] / A['n'], A['total_km'], len(A['misses'])))
    if a.compare:
        B = evaluate(a.compare, T, names, a.sample)
        print('=== %s ===' % os.path.basename(B['path']))
        print('  routed %d/%d pairs (%.0f%%), summed distance %.0f km, %d unroutable'
              % (B['routed'], B['n'], 100.0 * B['routed'] / B['n'], B['total_km'], len(B['misses'])))
        # Compare distance ONLY over pairs routable in both.  Summing all
        # routable pairs is misleading: newly-routable pairs add distance
        # without any route getting worse, which reads as a regression.
        common = [k for k in B['per_pair'] if B['per_pair'][k] is not None
                  and A['per_pair'].get(k) is not None]
        cb = sum(B['per_pair'][k] for k in common)
        ca = sum(A['per_pair'][k] for k in common)
        shorter = sum(1 for k in common if A['per_pair'][k] < B['per_pair'][k] - 1e-9)
        longer = sum(1 for k in common if A['per_pair'][k] > B['per_pair'][k] + 1e-9)
        print('=== delta (compare -> in) ===')
        print('  routed   %d -> %d  (%+d)' % (B['routed'], A['routed'], A['routed'] - B['routed']))
        print('  newly routable: %d   newly unroutable: %d'
              % (len([k for k in A['per_pair'] if A['per_pair'][k] is not None
                      and B['per_pair'].get(k) is None]),
                 len([k for k in B['per_pair'] if B['per_pair'][k] is not None
                      and A['per_pair'].get(k) is None])))
        print('  on the %d pairs routable in BOTH:' % len(common))
        print('     distance %.1f -> %.1f km  (%+.1f%%)'
              % (cb * 0.1, ca * 0.1, 100.0 * (ca - cb) / max(cb, 1e-9)))
        print('     shorter: %d   longer: %d   unchanged: %d'
              % (shorter, longer, len(common) - shorter - longer))


if __name__ == '__main__':
    main()
