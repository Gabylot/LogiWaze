#!/usr/bin/env python3
"""weld_network.py -- close endpoint gaps in a road network.

Routing joins by shared endpoint, so endpoints that stop short of each other
leave the network broken.  This closes those gaps, subject to checks that each
one is a real continuation rather than a coincidence.

Acceptance for a candidate pair
-------------------------------
1. different components      (otherwise there is nothing to fix)
2. mutual nearest cross-component match  (each is the other's best candidate)
3. join geometry             pass-through joins must be near straight; a
                             large turn means the two roads are a fork or a
                             hairpin, not a continuation
4. cross-hex joins must be    the two hexes must actually share an edge, and
   plausible                 the join must sit on that edge

Why not just a distance threshold: an early attempt with a 800 m threshold
welded 9,364 endpoints, joining roads that merely happened to be near each
other.  The checks above are what make a weld safe, not the threshold.

Usage:
    python3 weld_network.py --in road_source.geojson --out welded.geojson
"""
import argparse
import importlib.util
import json
import math
import os
from collections import Counter, defaultdict

import numpy as np

import graphkit as gk

HERE = os.path.dirname(os.path.abspath(__file__))


def load_grid():
    spec = importlib.util.spec_from_file_location('grid', os.path.join(HERE, 'grid.py'))
    grid = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(grid)
    grid.load_offsets(os.path.join(HERE, 'scripts', 'export_major_locations.sh'))
    return grid


class Hexes:
    """True hex geometry from grid.py's offset table.

    The grid packs diagonally at 0.75*W, not W/2 -- assuming W/2 made 90
    genuinely adjacent hex pairs look non-adjacent, which is how a correct
    dataset produced an alarming false conclusion.
    """

    def __init__(self, grid, observed=None):
        self.W, self.K = grid.W, grid.K
        self.centre = {}
        for h in grid.OFFSETS:
            ox, oy = grid.hex_origin(h)
            self.centre[h] = (ox + self.W / 2.0, oy - self.K / 2.0)
        # `observed` is deliberately IGNORED.  It was added to let pak-derived
        # hexes override the grid, on the theory that a hex's own road locates
        # it better than the offset table.  That is the same mistake as fitting
        # WORLD_ORIGIN to per-hex road centroids: road is not distributed
        # symmetrically inside a hex, so the median road vertex carries each
        # hex's road bias and using it as a centre makes adjacent hexes stop
        # looking adjacent.  It cost 127 cross-hex welds on the OCR network
        # (every one rejected 'non-adjacent-hex').  The grid is the authority for
        # hex LAYOUT; only the road within a hex comes from the data.
        self.observed = {}

    def centre_of(self, h):
        return self.centre.get(h)

    def relation(self, a, b):
        """Name the edge two hexes share, or None if they do not share one.

        A tolerance is required, not an exact match.  Hexes whose road came
        from the pak sit a little off the canonical grid (PariPeakHex by 3.6
        units, ~16% of a hex), and an exact offset test calls genuinely
        adjacent pairs unrelated -- which silently drops every cross-hex weld
        involving them.  The tolerance is a quarter of the tightest hex
        dimension, so it absorbs that scatter without admitting a pair that
        is really two rows away.
        """
        ca, cb = self.centre_of(a), self.centre_of(b)
        if ca is None or cb is None:
            return None
        dx, dy = abs(ca[0] - cb[0]), abs(ca[1] - cb[1])
        tol = 0.25 * min(self.W, self.K)
        for name, (ex, ey) in (('vertical', (0.0, self.K)),
                               ('horizontal', (self.W, 0.0)),
                               ('diagonal', (0.75 * self.W, self.K / 2.0))):
            if abs(dx - ex) < tol and abs(dy - ey) < tol:
                return name
        return None

    def edge_distance(self, p, a, b):
        """Perpendicular distance from p to the edge shared by hexes a and b."""
        ca, cb = self.centre_of(a), self.centre_of(b)
        if ca is None or cb is None:
            return math.inf
        mx, my = (ca[0] + cb[0]) / 2.0, (ca[1] + cb[1]) / 2.0
        ax, ay = ca[0] - cb[0], ca[1] - cb[1]
        n = math.hypot(ax, ay)
        if n < 1e-12:
            return 0.0
        return abs((p[0] - mx) * ay - (p[1] - my) * ax) / n


def apply_welds(lines, new, end_nodes):
    """Return new line vertex lists with welded endpoints moved to midpoints.

    `new` and `end_nodes` are keyed (line_idx, end) where end is 0 for the
    line's first vertex and 1 for its last.  An earlier version looked up key
    (li, -1) and so silently never moved a line's final vertex, welding half
    the endpoints and breaking the connections it meant to make.
    """
    out = []
    for li, (region, w) in enumerate(lines):
        pts = [list(p) for p in w]
        if (li, 0) in new:
            pts[0] = list(new[(li, 0)])
        if (li, 1) in new:
            pts[-1] = list(new[(li, 1)])
        out.append((region, pts))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--in', dest='src', required=True)
    ap.add_argument('--out', dest='dst', required=True)
    ap.add_argument('--max-gap', type=float, default=3.0, help='world units (100 m each)')
    ap.add_argument('--max-turn', type=float, default=60.0,
                    help='degrees; pass-through joins sharper than this are forks')
    ap.add_argument('--border-tol', type=float, default=1.0,
                    help='world units from the shared hex edge that a cross-hex weld may sit')
    ap.add_argument('--rounds', type=int, default=1,
                    help='iterations. 1 is deliberate: measured on this data, each extra round made the vertex model WORSE (91 -> 101 -> 103 comps), because later rounds only find marginal pairs that break as many junctions as they fix')
    ap.add_argument('--report')
    a = ap.parse_args()

    data = json.load(open(a.src))
    lines = gk.load(a.src).lines
    net = gk.Network(lines, path=a.src)
    # Hex centres come from the road data itself, not just the offset table: a
    # hex whose road came from the pak sits off the canonical grid, and
    # measuring a cross-hex join against the canonical border then rejects
    # genuine continuations (PariPeakHex -> KuuraStrandHex measures 1.9 units
    # from the canonical edge, so a 1.0-unit border tolerance refused real road).
    grid = Hexes(load_grid())
    print('start: %d lines, %d components' % (len(lines), net.n_components))

    # end_nodes[(line_idx, 0|1)] = target world coordinate
    end_nodes = {}
    total_reject = Counter()
    report = []
    for rnd in range(a.rounds):
        net = gk.Network(lines, path=a.src)
        misses = net.near_misses(a.max_gap)
        new = {}
        for gap, i, j in misses:
            ea, eb = net.ends[i], net.ends[j]
            ra, rb = ea[1], eb[1]
            kind = net.join_kind(i, j)
            if kind is None:
                total_reject['degenerate'] += 1
                continue
            kname, ang = kind
            if kname == 'pass-through' and ang > a.max_turn:
                total_reject['sharp-turn'] += 1
                continue
            if ra != rb:
                rel = grid.relation(ra, rb)
                if rel is None:
                    total_reject['non-adjacent-hex'] += 1
                    continue
                p = ((ea[0][0] + eb[0][0]) / 2.0, (ea[0][1] + eb[0][1]) / 2.0)
                if grid.edge_distance(p, ra, rb) > a.border_tol:
                    total_reject['far-from-border'] += 1
                    continue
            else:
                p = ((ea[0][0] + eb[0][0]) / 2.0, (ea[0][1] + eb[0][1]) / 2.0)
            la, wa = ea[2], 0 if ea[3] == 'S' else 1
            lb, wb = eb[2], 0 if eb[3] == 'S' else 1
            ka, kb = (la, wa), (lb, wb)
            if ka in end_nodes or kb in end_nodes:
                total_reject['endpoint-already-used'] += 1
                continue
            # Weld ASYMMETRICALLY.  Moving both endpoints to the midpoint
            # detaches each from any other line that shared its exact vertex,
            # which tears down more connections than the weld creates: doing
            # that dropped the vertex model from 113 components to 182.  So keep
            # the better-connected endpoint where it is and move only the
            # weaker one onto it.
            da, db = net.coincidence(i), net.coincidence(j)
            if da < db:
                src_w, keep = lines[la][1][0 if wa == 0 else -1], kb
                tgt_w = lines[lb][1][0 if wb == 0 else -1]
            else:
                src_w, keep = lines[lb][1][0 if wb == 0 else -1], ka
                tgt_w = lines[la][1][0 if wa == 0 else -1]
            # BOTH guards matter, and both need a NODE (rounded), not a raw
            # coordinate -- passing the raw tuple silently missed every lookup
            # and the guard never fired.
            #
            #   source: moving an endpoint that is currently joined to
            #           something else severs that join, which is how one
            #           previously-routable pair became unroutable.
            #   target: landing on a junction with more than one vertex there
            #           detaches the moved endpoint from the rest of it.
            if net.coincidence_at(gk.node_of(src_w[0], src_w[1])) > 1:
                total_reject['source-has-neighbours'] += 1
                continue
            if net.coincidence_at(gk.node_of(tgt_w[0], tgt_w[1])) > 1:
                total_reject['would-break-junction'] += 1
                continue
            new[keep] = (float(src_w[0]), float(src_w[1]))
            report.append(dict(round=rnd, gap=round(gap, 4), kind=kname,
                               angle=round(ang, 1), hex_a=ra, hex_b=rb,
                               cross=(ra != rb)))
        if not new:
            print('round %d: nothing further to weld' % rnd)
            break
        lines = apply_welds(lines, new, end_nodes)
        end_nodes.update(new)
        net = gk.Network(lines, path=a.src)
        vc, vl = net.vertex_components()
        print('round %d: welded %d endpoint pairs -> %d endpoint comps, '
              '%d vertex comps (largest %.1f%%)'
              % (rnd, len(new) // 2, net.n_components, vc, 100.0 * vl / sum(len(w) for _, w in lines)))

    print('rejected: %s' % dict(total_reject) if total_reject else 'rejected: none')
    print('total welds: %d pairs' % (len(end_nodes) // 2))

    # Write back the WELDED `lines`, converting world -> mercator.
    #
    # This must be `lines` -- the list the welds were applied to -- and not a
    # fresh gk.load(a.src).  Re-reading the source silently discarded every
    # weld: the run reported "159 vertex comps (largest 55.8%)" and then wrote a
    # file that still measured 324 comps / 5.5%, i.e. byte-equivalent to the
    # input.  Always re-measure the WRITTEN file, not the in-memory network.
    #
    # gk.load() DROPS lines shorter than two points, so the feature list and
    # the line list are not in 1:1 correspondence (the deployed file: 21273
    # features, 21254 lines).  Walking the features with one running index
    # over-runs the line list and would hand a short feature the NEXT feature's
    # geometry, so each feature is realigned against the same
    # "would this have been kept?" rule gk.load uses, and a feature
    # contributing no line passes through untouched.
    kept = lines
    ki = 0
    out = []
    fi = 0
    for f in data['features']:
        f = json.loads(json.dumps(f))
        g = f['geometry']
        multi = g['type'] == 'MultiLineString'
        cs = g['coordinates'] if multi else [g['coordinates']]
        newcs = []
        for c in cs:
            fi += 1
            if len(c) >= 2 and ki < len(kept):
                region, w = kept[ki]
                ki += 1
                newcs.append([list(gk.w2m(x, y)) for x, y in w])
            else:
                newcs.append(c)
        f['geometry']['coordinates'] = newcs if multi else newcs[0]
        out.append(f)
    assert ki == len(kept), \
        'line count mismatch: consumed %d of %d' % (ki, len(kept))
    data['features'] = out
    json.dump(data, open(a.dst, 'w'))
    print('wrote %s (%d features, %d lines, %d degenerate passthrough)'
          % (a.dst, len(out), ki, fi - ki))
    if a.report:
        json.dump(report, open(a.report, 'w'), indent=1)
        print('wrote %s (%d welds)' % (a.report, len(report)))


if __name__ == '__main__':
    main()
