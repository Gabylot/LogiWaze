#!/usr/bin/env python3
"""graph_audit.py -- measure a road network's structural health.

Usage:
    python3 graph_audit.py --in road_source.geojson
    python3 graph_audit.py --in a.geojson --compare b.geojson
    python3 graph_audit.py --in a.geojson --json report.json

The graph model lives in graphkit.py, covered by test_graphkit.py.

  components  connected pieces counting only endpoint-to-endpoint joins.  A
              road ending mid-way through another road is NOT a connection,
              because routing that joins on endpoints will not find it.  Large
              numbers here are the real problem.

  near-miss   endpoints in DIFFERENT components, close together, that a
              correct weld would join.  This is the actionable list.
"""
import argparse
import json
import os
from collections import Counter

import numpy as np

import graphkit as gk

BUCKETS = (0.05, 0.15, 0.5, 1.0, 3.0)      # world units; 1 unit = 100 m


def load_towns(path):
    if not os.path.exists(path):
        return []
    return list(json.load(open(path, encoding='utf-8', errors='replace')).values())


def town_reach(net, towns, frame=(128.25, -128.25)):
    """(distance_m, component, name) per town, via the nearest road SEGMENT."""
    if not towns or not net.segs:
        return []
    A = np.array([s[0] for s in net.segs])
    B = np.array([s[1] for s in net.segs])
    d = B - A
    L2 = (d * d).sum(1)
    L2[L2 == 0] = 1e-12
    out = []
    for t in towns:
        Q = np.array([t['x'] + frame[0], t['y'] + frame[1]])
        tt = (((Q - A) * d).sum(1) / L2).clip(0, 1)
        proj = A + tt[:, None] * d
        dist = np.hypot(*(Q - proj).T)
        i = int(dist.argmin())
        out.append((float(dist[i]) * 100.0, net.comp_at(*proj[i])[0], t.get('name', '?')))
    return out


def audit(path, max_gap):
    net = gk.load(path)
    misses = net.near_misses(max_gap)
    edges = (0.0,) + BUCKETS + (float('inf'),)
    rows = []
    for lo, hi in zip(edges, edges[1:]):
        sel = [m for m in misses if lo <= m[0] < hi]
        if not sel:
            continue
        kinds, cross = Counter(), 0
        for gap, i, j in sel:
            k = net.join_kind(i, j)
            kinds[k[0] if k else '?'] += 1
            if net.ends[i][1] != net.ends[j][1]:
                cross += 1
        rows.append(dict(lo=lo, hi=hi, n=len(sel), cross=cross,
                         intra=len(sel) - cross, kinds=dict(kinds)))
    return net, misses, rows


def show(net, misses, rows, reach, label):
    print('=== %s ===' % label)
    print('  %d lines, %d segments, %d endpoints, %.1f km'
          % (len(net.lines), len(net.segs), len(net.ends), net.total_length * 0.1))
    print('  ENDPOINT model (strict): components %d, largest %d endpoints (%.1f%%)'
          % (net.n_components, net.largest_component,
             100.0 * net.largest_component / max(len(net.ends), 1)))
    vc, vl = net.vertex_components()
    print('  VERTEX   model (what the browser runs): components %d, largest %d vertices (%.1f%%)'
          % (vc, vl, 100.0 * vl / max(sum(len(w) for _, w in net.lines), 1)))
    if rows:
        print('  near-miss endpoint pairs (different components, not joined):')
        print('    %-14s %7s %7s %7s   %s' % ('gap (m)', 'total', 'cross', 'intra', 'kind'))
        for r in rows:
            hi = '>300' if r['hi'] == float('inf') else '%d-%d' % (r['lo'] * 100, r['hi'] * 100)
            kinds = ','.join('%s:%d' % kv for kv in sorted(r['kinds'].items()))
            print('    %-14s %7d %7d %7d   %s' % (hi, r['n'], r['cross'], r['intra'], kinds))
        print('    %-14s %7d' % ('TOTAL', sum(r['n'] for r in rows)))
    else:
        print('  near-miss endpoint pairs: none within %g units' % max_gap)
    if reach:
        d = np.array([x[0] for x in reach])
        print('  towns: %d | within 50 m %d (%.0f%%) | within 100 m %d (%.0f%%) | stranded>100 m %d'
              % (len(d), (d <= 50).sum(), 100 * (d <= 50).mean(),
                 (d <= 100).sum(), 100 * (d <= 100).mean(), (d > 100).sum()))
    print()


def summary(net, misses):
    vc, vl = net.vertex_components()
    return dict(path=net.path, components=net.n_components,
                largest_endpoint=net.largest_component,
                vertex_components=vc, largest_vertex=vl,
                lines=len(net.lines), segments=len(net.segs),
                length_km=net.total_length * 0.1, near_misses=len(misses))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--in', dest='path', required=True)
    ap.add_argument('--compare')
    ap.add_argument('--towns', default='towns.json')
    ap.add_argument('--max-gap', type=float, default=3.0)
    ap.add_argument('--json')
    a = ap.parse_args()

    towns = load_towns(a.towns)
    net, misses, rows = audit(a.path, a.max_gap)
    show(net, misses, rows, town_reach(net, towns), os.path.basename(a.path))
    out = [summary(net, misses)]
    if a.compare:
        net2, m2, r2 = audit(a.compare, a.max_gap)
        show(net2, m2, r2, town_reach(net2, towns), os.path.basename(a.compare))
        out.append(summary(net2, m2))
        print('=== delta (compare -> in) ===')
        print('  endpoint-model components %d -> %d' % (net2.n_components, net.n_components))
        print('  vertex-model  components %d -> %d' % (net2.vertex_components()[0], net.vertex_components()[0]))
        print('  near-miss pairs %d -> %d' % (len(m2), len(misses)))
    if a.json:
        for r in rows:
            r['lo'], r['hi'] = str(r['lo']), str(r['hi'])
        json.dump(dict(networks=out, buckets=rows), open(a.json, 'w'), indent=1)
        print('wrote %s' % a.json)


if __name__ == '__main__':
    main()
