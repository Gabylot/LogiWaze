#!/usr/bin/env python3
"""benchmark.py -- route a fixed pair set across a network and save results.

Run ONE network per process and write the distances to JSON, then compare with
--report.  Holding two networks at once is what made an earlier run exhaust
the box's swap; this keeps peak memory to a single graph.

Both the network and the town set are restricted to hexes that appear in the
hand-traced file, so every pair is meaningful for both.

    python3 benchmark.py --in a.geojson --hexes hexes.txt --out a.json
    python3 benchmark.py --in b.geojson --hexes hexes.txt --out b.json
    python3 benchmark.py --report a.json b.json
"""
import argparse
import json
import os
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import graphkit as gk
from route_test import attach_town, dijkstra, vertex_graph, towns, lookup, _fold

FRAME = (128.25, -128.25)


def hexes_in(path):
    return {f['properties'].get('region', '?') for f in json.load(open(path))['features']}


def pick_towns(T, allowed, n, seed=11):
    """Deterministic sample of town names spread across the allowed hexes."""
    import random
    by_hex = defaultdict(list)
    for nm, v in T.items():
        r = v.get('region')
        if r in allowed and nm.strip():
            by_hex[r].append(nm)
    rnd = random.Random(seed)
    pool = sorted(by_hex)
    rnd.shuffle(pool)
    out = []
    for h in pool:                      # round-robin so every hex is represented
        if by_hex[h]:
            out.append(rnd.choice(sorted(by_hex[h])))
    rnd.shuffle(out)
    return out[:n]


def run(path, out, allowed, anchors, towns_file):
    T = towns(towns_file)
    net = gk.load(path)
    adj = vertex_graph(net)
    names = pick_towns(T, allowed, anchors)
    attached = {}
    for nm in names:
        attached[nm] = attach_town(adj, net, lookup(T, nm), nm)
    results = {}
    for src in names:                    # one Dijkstra per anchor, reused for all targets
        dist = dijkstra(adj, attached[src][0])
        for dst in names:
            results['%s\t%s' % (src, dst)] = dist.get(attached[dst][0])
    payload = dict(path=path, anchors=names, hexes=len({T[n]['region'] for n in names}),
                   lines=len(net.lines), length_km=net.total_length * 0.1,
                   results=results)
    json.dump(payload, open(out, 'w'))
    routed = sum(1 for v in results.values() if v is not None)
    print('%s: %d anchors in %d hexes, %d pairs, routed %d (%.1f%%)'
          % (os.path.basename(path), len(names), payload['hexes'], len(results),
             routed, 100.0 * routed / len(results)))


def report(a_file, b_file):
    A = json.load(open(a_file))
    B = json.load(open(b_file))
    ka, kb = set(A['results']), set(B['results'])
    common = sorted(ka & kb)
    print('=== %s ===' % os.path.basename(A['path']))
    print('  %d lines, %.1f km' % (A['lines'], A['length_km']))
    print('=== %s ===' % os.path.basename(B['path']))
    print('  %d lines, %.1f km' % (B['lines'], B['length_km']))
    ra = {k: A['results'][k] for k in common if A['results'][k] is not None}
    rb = {k: B['results'][k] for k in common if B['results'][k] is not None}
    both = sorted(set(ra) & set(rb))
    da = sum(ra[k] for k in both)
    db = sum(rb[k] for k in both)
    newa = [k for k in common if A['results'][k] is not None and B['results'][k] is None]
    newb = [k for k in common if B['results'][k] is not None and A['results'][k] is None]
    shorter = sum(1 for k in both if ra[k] < rb[k] - 1e-9)
    longer = sum(1 for k in both if ra[k] > rb[k] + 1e-9)
    print('=== comparison on %d ordered pairs ===' % len(common))
    print('  routable   A %d (%.1f%%)   B %d (%.1f%%)'
          % (len(ra), 100.0 * len(ra) / len(common), len(rb), 100.0 * len(rb) / len(common)))
    print('  only A routable: %d      only B routable: %d' % (len(newa), len(newb)))
    print('  routable in BOTH: %d' % len(both))
    print('     total distance  A %.0f km   B %.0f km   A is %+.1f%%'
          % (da * 0.1, db * 0.1, 100.0 * (da - db) / max(db, 1e-9)))
    print('     A shorter: %d   A longer: %d   identical: %d'
          % (shorter, longer, len(both) - shorter - longer))
    if both:
        ratios = sorted(ra[k] / max(rb[k], 1e-9) for k in both)
        n = len(ratios)
        print('     per-pair ratio A/B:  p10 %.2f  median %.2f  p90 %.2f  max %.2f'
              % (ratios[int(0.10 * n)], ratios[n // 2], ratios[int(0.90 * n)], ratios[-1]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--in', dest='path')
    ap.add_argument('--hexes', help='file with one region name per line')
    ap.add_argument('--out')
    ap.add_argument('--anchors', type=int, default=60)
    ap.add_argument('--towns', default='towns.json')
    ap.add_argument('--report', nargs=2, metavar=('A', 'B'))
    a = ap.parse_args()
    if a.report:
        return report(*a.report)
    allowed = None
    if a.hexes:
        allowed = {l.strip() for l in open(a.hexes) if l.strip()}
    else:
        allowed = hexes_in(a.path)
    run(a.path, a.out, allowed, a.anchors, a.towns)


if __name__ == '__main__':
    main()
