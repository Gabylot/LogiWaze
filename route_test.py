#!/usr/bin/env python3
"""route_test.py -- shortest-path checks against town names.

Uses the VERTEX model, i.e. the graph leaflet-routing-machine builds: every
polyline waypoint is a node, so two lines connect when they share any vertex.
Endpoint-only connectivity is also reported, because a network that only
holds together through shared interior vertices is fragile.

    python3 route_test.py --in road_source.geojson
    python3 route_test.py --in a.geojson --compare b.geojson
"""
import argparse
import heapq
import json
import os
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import graphkit as gk

# Pairs that must work, plus the pair the maintainer reported as a detour.
PAIRS = [
    ('Pariah', 'Martti'),
    ('East Narthex', 'Transept'),
    ('Swordfort', 'Silk Farms'),
    ('Therizo', 'The Treasury'),
    ('Martyr\'s Fang', 'Rising Loom'),
    ('Eastmarch', 'Brine Glen'),
    ('The Salt Farms', 'Thunderfoot'),
]
FRAME = (128.25, -128.25)


def _fold(s):
    """Accent-insensitive, case-insensitive key.

    towns.json holds 'Therizo' with a combining acute; looking it up as
    'Therizo' silently reported the town as not found and quietly dropped the
    one route that had actually been reported as broken.
    """
    import unicodedata
    s = unicodedata.normalize('NFKD', s or '')
    return ''.join(c for c in s if not unicodedata.combining(c)).strip().lower()


def towns(path):
    d = json.load(open(path, encoding='utf-8', errors='replace'))
    out = {}
    for v in d.values():
        nm = (v.get('name') or '').strip()
        if nm:
            out.setdefault(nm, v)
    return out


def lookup(T, name):
    exact = T.get(name)
    if exact:
        return exact
    k = _fold(name)
    for nm, v in T.items():
        if _fold(nm) == k:
            return v
    return None


def vertex_graph(net):
    adj = defaultdict(list)
    for region, w in net.lines:
        for i in range(len(w) - 1):
            a, b = gk.node_of(*w[i]), gk.node_of(*w[i + 1])
            la = math_dist(w[i], w[i + 1])
            adj[a].append((b, la, region))
            adj[b].append((a, la, region))
    return adj


def math_dist(a, b):
    return float(np.hypot(a[0] - b[0], a[1] - b[1]))


def dijkstra(adj, src):
    """Single-source shortest paths.

    The heap carries a tie-breaking counter, not the node: without it Python
    compares nodes when costs are equal, and coordinate tuples cannot be
    ordered against each other.
    """
    INF = float('inf')
    dist = {src: 0.0}
    counter = 0
    pq = [(0.0, counter, src)]
    while pq:
        c, _n, u = heapq.heappop(pq)
        if c > dist.get(u, INF):
            continue
        for v, L, _r in adj[u]:
            nc = c + L
            if nc < dist.get(v, INF):
                dist[v] = nc
                counter += 1
                heapq.heappush(pq, (nc, counter, v))
    return dist


def attach_town(adj, net, t, label):
    """Snap a town to the nearest road SEGMENT and hang it off the graph.

    Using the segment's first vertex as the start point made before/after
    comparisons meaningless: welding moves geometry, so the nearest segment
    changes and the measured start point jumps.  Projecting onto the segment
    and inserting a virtual node joined to both of the segment's endpoints by
    their true distances keeps the comparison stable and physically correct.
    """
    x, y = t['x'] + FRAME[0], t['y'] + FRAME[1]
    dist, seg = net.nearest_seg(x, y)
    a, b = seg[0], seg[1]
    v = float(np.hypot(b[0] - a[0], b[1] - a[1]))
    if v < 1e-12:
        proj = np.array([a[0], a[1]])
    else:
        t_ = ((np.array([x, y]) - a) @ (b - a)) / (v * v)
        t_ = max(0.0, min(1.0, float(t_)))
        proj = a + t_ * (b - a)
    node = ('town', label)
    la = float(np.hypot(*(proj - a)))
    lb = float(np.hypot(*(b - proj)))
    na, nb = gk.node_of(*a), gk.node_of(*b)
    adj.setdefault(node, [])
    adj[node].append((na, la, 'town'))
    adj.setdefault(na, []).append((node, la, 'town'))
    if lb > 1e-9:
        adj[node].append((nb, lb, 'town'))
        adj.setdefault(nb, []).append((node, lb, 'town'))
    return node, dist * 100.0


def run(path, T, label):
    net = gk.load(path)
    adj = vertex_graph(net)
    vc, vl = net.vertex_components()
    print('=== %s ===' % label)
    print('  %d lines, %.1f km | vertex comps %d, largest %.1f%% | endpoint comps %d'
          % (len(net.lines), net.total_length * 0.1, vc,
             100.0 * vl / max(sum(len(w) for _, w in net.lines), 1),
             net.n_components))
    results = {}
    cache = {}
    for a, b in PAIRS:
        ta, tb = lookup(T, a), lookup(T, b)
        if not ta or not tb:
            print('  %-14s -> %-14s  (town not found)' % (a, b))
            continue
        if a not in cache:
            cache[a] = attach_town(adj, net, ta, a)
        if b not in cache:
            cache[b] = attach_town(adj, net, tb, b)
        na, da = cache[a]
        nb, db = cache[b]
        dist = dijkstra(adj, na)
        if nb in dist:
            results[(a, b)] = dist[nb]
            print('  %-14s -> %-14s  %6.2f km   (%.0f m, %.0f m from road)'
                  % (a, b, dist[nb] * 0.1, da, db))
        else:
            results[(a, b)] = None
            print('  %-14s -> %-14s  NO ROUTE   (%.0f m, %.0f m from road)'
                  % (a, b, da, db))
    print()
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--in', dest='path', required=True)
    ap.add_argument('--compare')
    ap.add_argument('--towns', default='towns.json')
    a = ap.parse_args()
    T = towns(a.towns)
    A = run(a.path, T, os.path.basename(a.path))
    if a.compare:
        B = run(a.compare, T, os.path.basename(a.compare))
        print('=== delta (compare -> in): route distance ===')
        for k in A:
            x, y = B.get(k), A.get(k)
            if x is None and y is None:
                continue
            if x is None:
                print('  %-14s -> %-14s  NO ROUTE -> %.2f km  (fixed)' % (k[0], k[1], y * 0.1))
            elif y is None:
                print('  %-14s -> %-14s  %.2f km -> NO ROUTE  (REGRESSED)' % (k[0], k[1], x * 0.1))
            else:
                d = (y - x) * 0.1
                print('  %-14s -> %-14s  %6.2f -> %6.2f km  (%+.2f)'
                      % (k[0], k[1], x * 0.1, y * 0.1, d))


if __name__ == '__main__':
    main()
