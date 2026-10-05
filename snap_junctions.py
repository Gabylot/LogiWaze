#!/usr/bin/env python3
"""snap_junctions.py -- connect roads that meet mid-line, not end to end.

WHY
---
`weld_network.py` only ever pairs one road's ENDPOINT with another road's
ENDPOINT.  That covers the pak's own spline chains, but the two sources in
`merge_roads.py` meet the way real junctions do: a road ENDS part-way along
another road.  graphkit.py's model is explicit that this is not a connection
("a road ending in the middle of another road is NOT a connection, because no
engine that joins on endpoints will find it"), so it must be made real in the
geometry: SPLIT the line being joined at the projection point, then move the
joining endpoint onto that new vertex.

The endpoint welder cannot do this.  On the merged pak+OCR network it rejected
2,178 candidates as 'source-has-neighbours' and 469 as 'would-break-junction' --
not because those joins are wrong, but because in a network dense with
junctions almost every endpoint already sits on a shared node.  Those are
mostly the T-junctions this tool resolves.

GUARDS
------
  * different components        -- nothing to fix otherwise
  * gap within --max-gap        -- world units
  * the projection is strictly INTERIOR, so splitting there cannot detach
    anything and cannot duplicate a vertex
  * heading is straight         -- a junction continues the road it lands on;
    a hairpin is not a continuation
  * cross-hex joins sit on the shared border, via the same Hexes relation and
    edge-distance checks weld_network uses

Splitting is idempotent, and the endpoint list is snapshotted before editing so
a vertex created by one snap is not immediately re-examined by the same pass.

Usage:
    python3 snap_junctions.py --in merged.geojson --out snapped.geojson
"""
import argparse
import json
import math
from collections import defaultdict

import numpy as np
from scipy.spatial import cKDTree

import graphkit as gk
import weld_network as wn


def build_segments(lines):
    """[(line_index, vertex_index, p, q)] for every segment of every line."""
    segs = []
    for idx, (region, w) in enumerate(lines):
        for i in range(len(w) - 1):
            segs.append((idx, i, np.asarray(w[i], float),
                         np.asarray(w[i + 1], float)))
    return segs


def midpoints(segs):
    if not segs:
        return np.zeros((0, 2))
    return np.array([(s[2] + s[3]) / 2.0 for s in segs])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="src", required=True)
    ap.add_argument("--out", dest="dst", required=True)
    ap.add_argument("--max-gap", type=float, default=0.5,
                    help="world units; 0.5 is 2%% of a hex")
    ap.add_argument("--max-turn", type=float, default=120.0,
                    help="degrees; a junction continues the road it lands on")
    ap.add_argument("--border-tol", type=float, default=2.0)
    ap.add_argument("--rounds", type=int, default=4)
    ap.add_argument("--report")
    a = ap.parse_args()

    grid = wn.Hexes(wn.load_grid())
    data = json.load(open(a.src))
    lines = gk.load(a.src).lines
    net = gk.Network(lines, path=a.src)
    print("start: %d lines, %d endpoint components"
          % (len(lines), net.n_components))

    report = []
    rejected = defaultdict(int)
    total = 0
    for rnd in range(a.rounds):
        net = gk.Network(lines, path=a.src)
        segs = build_segments(lines)
        if not segs:
            break
        tree = cKDTree(midpoints(segs))
        radius = a.max_gap * 2.0
        new = 0
        # Snapshot the endpoints: editing `lines` in place while iterating it
        # would make the cursor visit vertices this pass just created.
        ends = []
        for li, (region, w) in enumerate(lines):
            ends.append((li, 0, np.asarray(w[0], float), region))
            ends.append((li, -1, np.asarray(w[-1], float), region))

        for li, end, p, region in ends:
            if net.coincidence_at(gk.node_of(p[0], p[1])) > 1:
                rejected["endpoint-busy"] += 1
                continue
            d, si = tree.query(p, k=1, distance_upper_bound=radius)
            if not np.isfinite(d) or d > radius:
                rejected["too-far"] += 1
                continue
            tidx, vi, a0, a1 = segs[int(si)]
            if tidx == li:
                rejected["same-line"] += 1
                continue
            seg = a1 - a0
            den = float(seg @ seg)
            if den < 1e-18:
                rejected["degenerate"] += 1
                continue
            t = float((p - a0) @ seg) / den
            if t <= 0.0 or t >= 1.0:
                # projection lands on an existing vertex: that is an
                # endpoint-to-endpoint case, not a T-junction
                rejected["projection-at-vertex"] += 1
                continue
            proj = a0 + t * seg
            gap = float(np.hypot(*(p - proj)))
            if gap > a.max_gap:
                rejected["too-far"] += 1
                continue

            tregion, tw = lines[tidx]
            if region != tregion:
                if grid.relation(region, tregion) is None:
                    rejected["non-adjacent-hex"] += 1
                    continue
                mid = ((p[0] + proj[0]) / 2.0, (p[1] + proj[1]) / 2.0)
                if grid.edge_distance(mid, region, tregion) > a.border_tol:
                    rejected["far-from-border"] += 1
                    continue

            vin = np.asarray(tw[vi], float) - np.asarray(tw[vi - 1], float)
            vout = np.asarray(tw[vi + 1], float) - np.asarray(tw[vi], float)
            n1, n2 = float(np.linalg.norm(vin)), float(np.linalg.norm(vout))
            if n1 < 1e-12 or n2 < 1e-12:
                rejected["degenerate"] += 1
                continue
            ang = math.degrees(math.acos(max(-1.0, min(1.0, float(vin @ vout) / (n1 * n2)))))
            if ang > a.max_turn:
                rejected["sharp-turn"] += 1
                continue

            node = tuple(proj)
            lines[tidx] = (tregion, tw[:vi + 1] + [node] + tw[vi + 1:])
            lw = lines[li][1]
            if end == 0:
                lines[li] = (region, [node] + list(lw[1:]))
            else:
                lines[li] = (region, list(lw[:-1]) + [node])
            report.append(dict(round=rnd, gap=round(gap, 4), angle=round(ang, 1),
                               hex_a=region, hex_b=tregion,
                               cross=(region != tregion)))
            new += 1

        total += new
        print("round %d: snapped %d T-junctions" % (rnd, new))
        if new == 0:
            break
        segs = build_segments(lines)
        tree = cKDTree(midpoints(segs))

    net = gk.Network(lines, path=a.src)
    vc, vl = net.vertex_components()
    print("snapped %d T-junctions -> %d endpoint comps, %d vertex comps "
          "(largest %.1f%%)"
          % (total, net.n_components, vc,
             100.0 * vl / sum(len(w) for _, w in lines)))
    if rejected:
        print("rejected: %s" % dict(rejected))

    lines_by_feature = gk.load(a.src).lines
    ki = 0
    out = []
    for f in data["features"]:
        f = json.loads(json.dumps(f))
        g = f["geometry"]
        multi = g["type"] == "MultiLineString"
        cs = g["coordinates"] if multi else [g["coordinates"]]
        newcs = []
        for c in cs:
            if len(c) >= 2 and ki < len(lines_by_feature):
                region, w = lines_by_feature[ki]
                newcs.append([list(gk.w2m(x, y)) for x, y in w])
                ki += 1
            else:
                newcs.append(c)
        f["geometry"]["coordinates"] = newcs if multi else newcs[0]
        out.append(f)
    data["features"] = out
    json.dump(data, open(a.dst, "w"))
    print("wrote %s (%d features)" % (a.dst, len(out)))
    if a.report:
        json.dump(report, open(a.report, "w"), indent=1)


if __name__ == "__main__":
    main()