"""Weld road endpoints across hex borders so the network is one connected graph.

The problem
-----------
Roads are extracted per hex from a per-hex map, so each hex's roads stop at
its own border a few metres short of the neighbour's.  Routing engines join
roads by shared endpoints, so each hex becomes its own island and every
cross-hex route fails while intra-hex routes still work.

Why the first two attempts were wrong
--------------------------------------
* Threshold 1.0 world unit: closed 65 of ~108 crossings.  Single-border routes
  started working; multi-border routes did not.
* Threshold 8.0 with mutual-nearest-neighbour matching: welded 9364 endpoints.
  A wide threshold welds any endpoint that happens to be near a border,
  including roads that correctly dead-end mid-hex, and mutual-NN does not
  prevent it because two unrelated roads can be mutually nearest by accident.

The rule this version uses
--------------------------
A genuine crossing is not just "these two endpoints are near each other" - it is
"both endpoints sit ON the shared border between their two hexes".  So each
candidate is first gated on distance to the actual hex-border segment, computed
from the grid positions in MapStitcher/map.xml.  Only endpoints on the border
are eligible, which removes the 800m coincidence problem outright.  Among
eligible pairs the nearest is welded, and only when the choice is mutual.
"""
import argparse
import json
import math
import os
import re
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from scipy.spatial import cKDTree

import grid

HERE = os.path.dirname(os.path.abspath(__file__))
HALF_WORLD = 128.0
MERC_HALF = 20037500.0
# world -> mercator is a MULTIPLY.  This file originally had the inverse here,
# which is why its own world_to_merc below had to divide.  Both directions are
# named explicitly so the two cannot be confused again -- see pak_acceptance.py,
# where the same inversion produced a join tolerance 8600x too small.
SCALE = MERC_HALF / HALF_WORLD
W = grid.W
K = grid.K

BORDER_MARGIN = 0.9      # world units from the border line to be eligible (90 m)
MAX_GAP = 3.0            # max distance between two eligible endpoints (300 m)


def m2w_list(pts):
    return [[p[0] / SCALE + HALF_WORLD, p[1] / SCALE - HALF_WORLD] for p in pts]


def world_to_merc(pts):
    return [[(p[0] - HALF_WORLD) * SCALE, (p[1] + HALF_WORLD) * SCALE]
            for p in pts]


def grid_positions():
    """Hex CENTRES in world units, from grid.py's authoritative offset table.

    This replaces a second, hardcoded copy of the origin constants
    (ORIGIN_X/Y) plus a parse of MapStitcher/map.xml.  Two sources for the same
    thing is how the "+K/2 vs -K/2 sign error" the docstring mentions survived
    as long as it did.  grid.py already resolves the MapStitcher/table
    agreement and the region-spelling variants, so it is the only place this
    should come from.
    """
    grid.load_offsets(os.path.join(HERE, "scripts",
                                   "export_major_locations.sh"))
    pos = {}
    for name in grid.OFFSETS:
        ox, oy = grid.hex_origin(name)
        pos[name] = (ox + W / 2.0, oy - K / 2.0)
    return pos


def border_segment(ca, cb):
    """The edge two adjacent hexes share: through their midpoint, perpendicular
    to the line between their centres."""
    mid = ((ca[0] + cb[0]) / 2.0, (ca[1] + cb[1]) / 2.0)
    dx, dy = ca[0] - cb[0], ca[1] - cb[1]
    n = math.hypot(dx, dy)
    if n < 1e-9:
        return None
    # perpendicular
    px, py = -dy / n, dx / n
    half = W / 2.0
    return (mid[0] - px * half, mid[1] - py * half,
            mid[0] + px * half, mid[1] + py * half)


def dist_to_seg(p, seg):
    x0, y0, x1, y1 = seg
    dx, dy = x1 - x0, y1 - y0
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((p[0] - x0) * dx + (p[1] - y0) * dy) / L2))
    return math.hypot(p[0] - (x0 + t * dx), p[1] - (y0 + t * dy))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--in', dest='src',
                    default=os.path.join(HERE, 'pak_roads.geojson'),
                    help='input network. The default matches the filename this '
                         'tool has always used; pass --in explicitly for the '
                         'street build, which is a different file')
    ap.add_argument('--out', dest='dst', required=True)
    ap.add_argument('--margin', type=float, default=BORDER_MARGIN)
    ap.add_argument('--max-gap', type=float, default=MAX_GAP)
    ap.add_argument('--report', help='write a per-crossing report here')
    a = ap.parse_args()

    data = json.load(open(a.src))
    by = defaultdict(list)
    for f in data['features']:
        by[f['properties']['region']].append(f)
    pos = grid_positions()

    # world-space lines per hex
    lines = {}
    for h, fs in by.items():
        ls = []
        for f in fs:
            g = f['geometry']
            cs = g['coordinates'] if g['type'] == 'MultiLineString' else [g['coordinates']]
            for ln in cs:
                if len(ln) >= 2:
                    ls.append(m2w_list(ln))
        lines[h] = ls

    # endpoints, per hex
    ends = defaultdict(list)   # hex -> [(line_idx, 0|-1, point)]
    for h, ls in lines.items():
        for k, ln in enumerate(ls):
            ends[h].append((k, 0, np.array(ln[0])))
            ends[h].append((k, -1, np.array(ln[-1])))

    # adjacent hexes that BOTH have roads
    hexes = [h for h in lines if h in pos]
    proposals = {}   # (hexA,idxA) -> (gap, hexB, idxB, pointB, border)
    for i, ha in enumerate(hexes):
        for hb in hexes[i + 1:]:
            if ha not in ends or hb not in ends:
                continue
            seg = border_segment(pos[ha], pos[hb])
            if seg is None:
                continue
            # Adjacency test.  The original used `and`, which only rejects a pair
            # when BOTH axes are too far apart -- so it accepted hexes offset
            # diagonally by a full step in each axis, and for the same-axis case
            # it accepted a vertical pair 1.0*K apart only because the x test
            # passed vacuously at 0. `or` is the correct test: a pair shares an
            # edge only if they are within one step on BOTH axes.
            dxh = abs(pos[ha][0] - pos[hb][0])
            dyh = abs(pos[ha][1] - pos[hb][1])
            if dxh > W + 1e-6 or dyh > K + 1e-6:
                continue
            A = [e for e in ends[ha] if dist_to_seg(e[2], seg) <= a.margin]
            B = [e for e in ends[hb] if dist_to_seg(e[2], seg) <= a.margin]
            if not A or not B:
                continue
            PA = np.array([e[2] for e in A])
            PB = np.array([e[2] for e in B])
            dd, ii = cKDTree(PB).query(PA)
            for k in np.nonzero(dd <= a.max_gap)[0]:
                key = (ha, A[k][0], A[k][1])
                d = float(dd[k])
                cur = proposals.get(key)
                if cur is None or d < cur[0]:
                    proposals[key] = (d, hb, B[int(ii[k])][0], B[int(ii[k])][1], np.array(B[int(ii[k])][2]))
                # also record the reverse direction so mutuality can be tested
                rkey = (hb, B[int(ii[k])][0], B[int(ii[k])][1])
                rcur = proposals.get(rkey)
                if rcur is None or d < rcur[0]:
                    proposals[rkey] = (d, ha, A[k][0], A[k][1], np.array(A[k][2]))

    # keep only mutual proposals
    welded = set()
    moved = 0
    for key, (d, hb, kb, ib, pb) in sorted(proposals.items()):
        ha, ka, ia = key
        if key in welded:
            continue
        rkey = (hb, kb, ib)
        if rkey not in proposals:
            continue
        d2, ha2, ka2, ia2, pa2 = proposals[rkey]
        if ha2 != ha or (ka2, ia2) != (ka, ia):
            continue
        welded.add(key); welded.add(rkey)
        mid = ((pa2[0] + pb[0]) / 2.0, (pa2[1] + pb[1]) / 2.0)
        lines[ha][ka][ia] = list(mid)
        lines[hb][kb][ib] = list(mid)
        moved += 2

    print('welded %d endpoint pairs (%d endpoints) across %d borders'
          % (moved // 2, moved, len(welded) // 2))

    if a.report:
        rows = []
        for key, (d, hb, kb, ib, pb) in sorted(proposals.items()):
            rows.append(dict(hexA=key[0], hexB=hb, gap=round(d, 4),
                             welded=key in welded))
        json.dump(rows, open(a.report, 'w'), indent=1)
        nw = sum(1 for r in rows if r['welded'])
        print('report: %d candidate pairs, %d welded, %d left open'
              % (len(rows), nw, len(rows) - nw))

    # Write back from the SNAPPED geometry, preserving each feature's original
    # geometry type.  This build emits LineString (not MultiLineString), and the
    # previous version unconditionally wrote a MultiLineString-shaped payload,
    # which would have silently changed the file's schema.
    #
    # Lines are re-welded onto features by matching each snapped line back to
    # the feature it came from, rather than by a running per-hex counter: the
    # counter assumed every feature contributes exactly one line in order, and
    # it desynchronises the moment one feature is skipped.
    owner = {}      # (hex, line_idx) -> (feature_idx, sub_idx)
    k = defaultdict(int)
    for fi, f in enumerate(data['features']):
        h = f['properties']['region']
        g = f['geometry']
        cs = g['coordinates'] if g['type'] == 'MultiLineString' \
            else [g['coordinates']]
        for si, _c in enumerate(cs):
            if len(_c) >= 2:
                owner[(h, k[h])] = (fi, si)
                k[h] += 1

    out = [dict(f) for f in data['features']]
    for (h, li), (fi, si) in owner.items():
        wl = lines[h][li] if h in lines and li < len(lines[h]) else None
        if wl is None:
            continue
        f = out[fi]
        g = f['geometry']
        # world_to_merc returns a list of points; the whole LINE is needed, not
        # its first point.  Indexing [0] here produced a bare [x, y] in place of
        # the coordinate list, which desynchronises the file from the second
        # vertex onward -- it silently kept one point per feature.
        mc = world_to_merc(wl)
        if g['type'] == 'MultiLineString':
            g['coordinates'][si] = mc
        else:
            g['coordinates'] = mc
    data['features'] = out
    json.dump(data, open(a.dst, 'w'))
    print('wrote %s (%d features)' % (a.dst, len(out)))


if __name__ == '__main__':
    main()
