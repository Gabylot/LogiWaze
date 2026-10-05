#!/usr/bin/env python3
"""split_network.py -- connect roads that pass close by WITHOUT moving them.

Why this exists
---------------
`weld_network.py` works by moving an endpoint onto its partner.  That is safe
only when the endpoint is free to move, so it refuses any endpoint that
already touches another line -- 853 candidates on this data.  Those refusals
are why routes like The Salt Farms -> Thunderfoot fail with a 12.9 m
unconnected gap that is present, correct, and simply not joined.

The operation that works there is ADDITIVE rather than destructive: insert a
new vertex into both lines at the meeting point.  Nothing that was already
connected gets detached, because nothing moves.

Acceptance for a pair
---------------------
1. different components                    (there is a gap to close)
2. mutual nearest                          (each is the best candidate for the
                                            other, so one road cannot claim
                                            dozens of neighbours)
3. the lines are not near-parallel         (two parallel carriageways 12 m
                                            apart are two roads, not one)
4. within --max-gap                        (and the gap bounds how much road
                                            the inserted vertex can add)

Usage:
    python3 split_network.py --in road_source.geojson --out split.geojson
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


def point_seg_dist(P, A, B):
    """Distance from point P to segment AB, and the closest point on AB."""
    d = B - A
    L2 = float((d * d).sum())
    if L2 < 1e-18:
        return float(np.hypot(*(P - A))), A.copy()
    t = max(0.0, min(1.0, float((P - A) @ d) / L2))
    Q = A + t * d
    return float(np.hypot(*(P - Q))), Q


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--in', dest='src', required=True)
    ap.add_argument('--out', dest='dst', required=True)
    ap.add_argument('--max-gap', type=float, default=0.30, help='world units; 0.30 = 30 m')
    ap.add_argument('--min-angle', type=float, default=30.0,
                    help='degrees; below this the two lines are parallel')
    ap.add_argument('--knn', type=int, default=24)
    ap.add_argument('--per-end', type=int, default=2,
                    help='joins allowed per endpoint; a junction has several arms')
    ap.add_argument('--report')
    a = ap.parse_args()

    net = gk.load(a.src)
    lines = [(r, [list(p) for p in w]) for r, w in net.lines]
    n_lines = len(lines)

    # vertex table and per-vertex component
    vpos, vline, vidx = [], [], []
    for li, (r, w) in enumerate(net.lines):
        for vi, p in enumerate(w):
            vpos.append(p); vline.append(li); vidx.append(vi)
    vpos = np.array(vpos, dtype=float)
    vline = np.array(vline)
    vcomp = np.empty(len(vpos), dtype=np.int64)
    for k, (li, vi) in enumerate(zip(vline, vidx)):
        vcomp[k] = hash(net.vertex_comp(int(li), int(vi)))

    # candidate: for each ENDPOINT, the nearest point on any different-component line
    ends = [i for i, e in enumerate(net.ends)]
    epos = np.array([(e[0][0], e[0][1]) for e in net.ends], dtype=float)
    # Component ids must come from ONE model.  Using the endpoint model for
    # endpoints and the vertex model for vertices compares two unrelated hash
    # spaces, so "different component" becomes meaningless -- which is what
    # produced 1793 joins with a median gap of 0.00, i.e. pairs that are
    # already coincident.
    eline = np.array([e[2] for e in net.ends])
    ewhich = np.array([0 if e[3] == 'S' else 1 for e in net.ends])
    ecomp = np.array([hash(net.vertex_comp(int(e[2]), 0 if e[3] == 'S' else len(net.lines[e[2]][1]) - 1))
                      for e in net.ends], dtype=np.int64)

    # segment arrays for the whole network, for projection
    segA, segB, segL = [], [], []
    for li, (r, w) in enumerate(net.lines):
        for i in range(len(w) - 1):
            segA.append(w[i]); segB.append(w[i + 1]); segL.append(li)
    segA = np.array(segA, dtype=float); segB = np.array(segB, dtype=float)
    segL = np.array(segL)
    # vertex index of each segment's first point, to get its component
    segC = np.array([vcomp[0] for _ in range(len(segA))])
    vindex = {}
    for k, (li, vi) in enumerate(zip(vline, vidx)):
        vindex[(int(li), int(vi))] = k
    for si in range(len(segA)):
        li = int(segL[si])
        vi = 0
        while vi + 1 < len(net.lines[li][1]) and not (
                abs(net.lines[li][1][vi][0] - segA[si][0]) < 1e-12
                and abs(net.lines[li][1][vi][1] - segA[si][1]) < 1e-12):
            vi += 1
        segC[si] = vcomp[vindex[(li, vi)]]

    tree = cKDTree(vpos)
    kk = min(a.knn, len(vpos))
    d, idx = tree.query(epos, k=kk)

    # Which node pairs are ALREADY directly joined?  Near-coincidence is not
    # the question -- "are these two an edge?" is.  An endpoint 6 m from
    # another line's interior vertex may be in the SAME component as it and
    # still not joined: the component reaches it the long way round, and
    # joining them is precisely the shortcut being looked for.  Requiring a
    # different component, as an earlier version did, threw those away and
    # that is what left Salt Farms -> Thunderfoot on a 7.5 km detour.
    already = set()
    for r, w in net.lines:
        for q in range(len(w) - 1):
            a1 = gk.node_of(*w[q]); b1 = gk.node_of(*w[q + 1])
            already.add((a1, b1) if a1 <= b1 else (b1, a1))

    best = {}
    cands = {}
    total_reject = {}
    for i in range(len(ends)):
        for c in range(1, kk):
            j = int(idx[i][c])
            lj = int(vline[j])
            if lj == int(eline[i]):
                continue
            vj = int(vidx[j])
            epos_i = epos[i]
            # already directly joined to the host line?  then nothing to do
            host = gk.node_of(*net.lines[lj][1][vj])
            me = gk.node_of(epos_i[0], epos_i[1])
            if ((me, host) if me <= host else (host, me)) in already:
                continue
            if float(np.hypot(*(epos[i] - vpos[j]))) > a.max_gap:
                break
            # distance from endpoint i to the LINE containing vertex j
            wj = net.lines[lj][1]
            vj = int(vidx[j])
            if vj + 1 < len(wj):
                dist, Q = point_seg_dist(epos[i], np.array(wj[vj], dtype=float), np.array(wj[vj + 1], dtype=float))
                segdir = np.array(wj[vj + 1], dtype=float) - np.array(wj[vj], dtype=float)
                hit = vj
            else:
                dist, Q = point_seg_dist(epos[i], np.array(wj[vj - 1], dtype=float), np.array(wj[vj], dtype=float))
                segdir = np.array(wj[vj], dtype=float) - np.array(wj[vj - 1], dtype=float)
                hit = vj - 1
            if dist > a.max_gap:
                continue
            # Prefer a candidate that actually MERGES two components.  Taking
            # the single closest candidate wasted the join: at the junction west
            # of Salt Farms, line 719 was 2.6 m away and already ran through
            # that exact point, so it won and the 5.5 m link to line 712 - the
            # one that opens the southern route to the border - was never
            # considered.  Same-component candidates are the fallback only.
            merges = (vcomp[j] != ecomp[i])
            cands.setdefault(i, []).append((dist, lj, hit, Q, segdir, merges))
        if cands.get(i):
            # Keep several candidates per endpoint.  A junction legitimately
            # joins more than one road, and taking only the closest wasted the
            # link: west of Salt Farms, line 719 (2.6 m, already running through
            # that point) beat line 712 (5.5 m, the road that leads south to the
            # border).  Prefer ones that merge components, then nearest, and
            # take up to --per-end so one road cannot claim a whole streetscape.
            cands[i].sort(key=lambda t: (0 if t[5] else 1, t[0]))
            best[i] = cands[i][0]

    # Conflict rule, NOT mutuality.
    #
    # The first version required endpoint i to be the best claimant on line lj
    # AND for line lj to have an endpoint claiming i back.  That is wrong: it
    # discards every T-junction, where a road ENDS on another road and the
    # host road has no endpoint anywhere near.  That is the dominant real case
    # -- Salt Farms -> Thunderfoot was broken by one, 5.5 m from a line's
    # interior vertex, and it was rejected because the host line's own
    # endpoints were 222 m and 613 m away so nothing "claimed" it back.
    #
    # What actually needs preventing is one endpoint grabbing many neighbours,
    # and many endpoints piling onto one spot of the same line.  So: each
    # endpoint joins at most once, and each target line receives at most one
    # join.
    line_best = {}
    for i, (dist, lj, hit, Q, segdir, _mg) in best.items():
        if lj not in line_best or dist < line_best[lj][0]:
            line_best[lj] = (dist, i)

    chosen = {}
    used_e, used_l = set(), []
    report = []
    ordered = []
    for i, cl in cands.items():
        for rank, c in enumerate(cl[:a.per_end]):
            ordered.append((rank, i) + c)
    ordered.sort(key=lambda t: (t[0], t[2]))
    for _rank, i, dist, lj, hit, Q, segdir, _m in ordered:
        if i in used_e:
            continue
        li = int(eline[i])
        wi = net.lines[li][1]
        ei = 0 if ewhich[i] == 0 else len(wi) - 1
        nbr = wi[1] if ewhich[i] == 0 else wi[-2]
        idir = np.array(wi[ei], dtype=float) - np.array(nbr, dtype=float)
        n1, n2 = np.hypot(*idir), np.hypot(*segdir)
        if n1 < 1e-12 or n2 < 1e-12:
            continue
        ang = math.degrees(math.acos(max(-1.0, min(1.0, float(idir @ segdir) / (n1 * n2)))))
        if ang < a.min_angle:
            total_reject['near-parallel'] = total_reject.get('near-parallel', 0) + 1
            continue
        # Conflict rule: a target line may receive several joins, but not two
        # at the same spot.  Refusing one join per LINE was too blunt -- a road
        # legitimately connects at several points, and that rule blocked the
        # link that Salt Farms -> Thunderfoot needs (704 -> 667, 17.9 m apart
        # at 65 degrees, which passes every other test).
        spot = (lj, hit)
        if spot in used_l:
            total_reject['spot-claimed'] = total_reject.get('spot-claimed', 0) + 1
            continue
        if lj in used_l and any(o[0] == lj and abs(o[1] - hit) <= 1 for o in used_l):
            total_reject['spot-claimed'] = total_reject.get('spot-claimed', 0) + 1
            continue
        used_e.add(i); used_l.append((lj, hit))
        # Insert Q -- the projection of the endpoint ONTO the host line -- not
        # the midpoint.  Q lies on the host road, so the new vertex does not
        # bend the host's own geometry; the moving line simply extends to meet
        # it, which is what a real T-junction is.  Inserting the midpoint puts
        # the vertex half a gap off the host's path, which showed up as an
        # 8 m north-and-back spike in the rendered route.
        M = (float(Q[0]), float(Q[1]))
        chosen[i] = (li, ewhich[i], M)
        chosen[('L', lj)] = (lj, hit, M)
        report.append(dict(gap=round(dist, 4), angle=round(ang, 1),
                           line_a=int(li), line_b=int(lj), hit_b=int(hit)))

    print('  rejected: %s' % total_reject)
    # Apply the inserts.  Vertex indices are computed BEFORE any insertion, so
    # inserting one shifts every later index on that line.  Applying them in
    # dict order therefore dropped later joins one vertex early, which is what
    # produced the 180-286 m phantom diagonals: a vertex landing beside the
    # wrong one joined two far-apart parts of the same road.  Collect per line
    # and insert back-to-front so each index is still valid when used.
    # Apply the inserts.  Vertex indices are computed BEFORE any insertion, so
    # inserting one shifts every later index on that line.  Applying them in
    # dict order therefore dropped later joins one vertex early, which is what
    # produced the 180-286 m phantom diagonals: a vertex landing beside the
    # wrong one joined two far-apart parts of the same road.  Collect per line
    # and insert back-to-front so each index is still valid when used.
    #
    # NOTE on the endpoint insert below, measured 2026-09-30.  Inserting Q
    # BEFORE the endpoint (position 0) leaves p0 in place, so the line reads
    # [Q, p0, p1, ...] and spends ~2x the gap of extra length per join - the
    # network grows 834.8 -> 902.0 km on the first pass.  That extra length is
    # most of the "two roads on one road" seen on the map (twin rate 14.4% ->
    # 26.3% across a split pass).
    #
    # Two alternatives were implemented and measured, and both are worse:
    #   * REPLACING the endpoint (w[pos] = Q) removes the dead length but severs
    #     every other road that met at p0 - a junction is exactly where p0
    #     matters.  East Narthex -> Transept went 2.47 -> 7.01 km and Salt Farms
    #     -> Thunderfoot stopped routing entirely.
    #   * Inserting Q just INSIDE the endpoint (position 1) keeps p0 and so keeps
    #     the junction, but still lays the line out to Q and back: 834.8 ->
    #     965.1 km, worse than the original.
    # The detour is inherent to bridging a gap at all, so the real fix is not to
    # join across gaps that large (--max-gap), not to reshape the insert.
    inserts = defaultdict(list)                  # line_idx -> [(position, point)]
    for i, (li, ewhich, M) in chosen.items():
        if isinstance(i, tuple):
            continue
        pos = 0 if ewhich == 0 else len(lines[li][1])
        inserts[li].append((pos, [float(M[0]), float(M[1])]))
    for k, (lj, hit, M) in chosen.items():
        if isinstance(k, tuple) and k[0] == 'L':
            inserts[lj].append((hit + 1, [float(M[0]), float(M[1])]))
    for lj, items in inserts.items():
        w = lines[lj][1]
        for pos, pt in sorted(items, key=lambda t: -t[0]):
            w.insert(pos, pt)

    print('split: %d joins' % (len(report)))
    new = gk.Network(lines, path=a.dst)
    vc, vl = new.vertex_components()
    print('  -> %d vertex comps, largest %d (%.1f%%)'
          % (vc, vl, 100.0 * vl / sum(len(w) for _, w in lines)))
    if report:
        angs = [r['angle'] for r in report]
        gaps = [r['gap'] for r in report]
        print('  gap: median %.2f u (%.0f m)  max %.2f u (%.0f m)'
              % (np.median(gaps), np.median(gaps) * 100, max(gaps), max(gaps) * 100))
        print('  angle: median %.0f deg  min %.0f' % (np.median(angs), min(angs)))

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
            r, w = lines[fi]; fi += 1
            newcs.append([list(gk.w2m(x, y)) for x, y in w])
        f['geometry']['coordinates'] = newcs if multi else newcs[0]
        out.append(f)
    assert fi == n_lines, 'feature/line mismatch %d vs %d' % (fi, n_lines)
    data['features'] = out
    json.dump(data, open(a.dst, 'w'))
    print('  wrote %s (%d features, %d vertices)' % (a.dst, len(out), sum(len(w) for _, w in lines)))
    if a.report:
        json.dump(report, open(a.report, 'w'), indent=1)


if __name__ == '__main__':
    main()
