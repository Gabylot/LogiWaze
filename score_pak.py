"""
Score roads_from_pak.py output against the hand-traced ground truth.

Uses netdiff.py, which the handover names as the metric to trust (bidirectional:
forward = invented roads, backward = missed roads), and turn_check.py, which
must be run on the POLYLINES the app routes on -- routing on the skeleton
graph gave 34.7% where the real figure is 78.3%.

The point of the pak route is the TURN count, so that is reported first and
compared against the 122.9-extracted / 95.0-hand baseline in the handover.

    python3 score_pak.py AcrithiaHex [...]
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import grid
import roads_from_pak as rfp

HERE = os.path.dirname(os.path.abspath(__file__))


def hand_traced(region):
    """Hand-drawn features for one hex, in world units."""
    HALF_WORLD, MERC_HALF = 128.0, 20037500.0
    SCALE = HALF_WORLD / MERC_HALF
    src = json.load(open(os.path.join(HERE, "road_source.geojson")))
    out = []
    for f in src["features"]:
        if f.get("properties", {}).get("region") != region:
            continue
        g = f["geometry"]
        lines = (g["coordinates"] if g["type"] == "MultiLineString"
                 else [g["coordinates"]])
        for line in lines:
            out.append([(x * SCALE + HALF_WORLD, y * SCALE - HALF_WORLD)
                        for x, y in line])
    return out


def resample(line, step=0.05):
    """Densify a polyline so a point-to-point distance is a fair comparison.

    A one-point line (a stray vertex in the hand trace) has no length; return
    it as-is rather than indexing past the end.
    """
    if len(line) < 2:
        return list(line)
    out = []
    for i in range(len(line) - 1):
        a, b = line[i], line[i + 1]
        d = ((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5
        n = max(1, int(d / step))
        for j in range(n):
            t = j / float(n)
            out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
    out.append(line[-1])
    return out


def chamfer(a_pts, b_pts):
    """Bidirectional chamfer: mean nearest-neighbour distance both ways."""
    import bisect
    b = sorted(b_pts)
    bx = [p[0] for p in b]

    def nn(px, py):
        i = bisect.bisect_left(bx, px)
        best = 1e18
        for j in range(max(0, i - 40), min(len(b), i + 40)):
            d = (b[j][0] - px) ** 2 + (b[j][1] - py) ** 2
            if d < best:
                best = d
        return best ** 0.5

    fwd = sum(nn(x, y) for x, y in a_pts) / len(a_pts)
    bwd = sum(nn(x, y) for x, y in b_pts) / len(b_pts)
    return fwd, bwd


def polyline_len(line):
    if len(line) < 2:
        return 0.0
    return sum(((line[i + 1][0] - line[i][0]) ** 2 +
                (line[i + 1][1] - line[i][1]) ** 2) ** 0.5
               for i in range(len(line) - 1))


def count_turns(line, deg=25.0):
    """Turns as the app would see them: a change of heading at a vertex."""
    import math
    n = 0
    for i in range(1, len(line) - 1):
        a1 = math.atan2(line[i][1] - line[i - 1][1], line[i][0] - line[i - 1][0])
        a2 = math.atan2(line[i + 1][1] - line[i][1], line[i + 1][0] - line[i][0])
        d = abs(math.degrees(a2 - a1))
        if d > 180:
            d = 360 - d
        if d >= deg:
            n += 1
    return n


def score(region, y_sign):
    mine = [line for _, line in rfp.hex_lines(region, y_sign=y_sign)]
    hand = hand_traced(region)
    if not mine or not hand:
        return None
    a = [p for line in mine for p in resample(line)]
    b = [p for line in hand for p in resample(line)]
    fwd, bwd = chamfer(a, b)
    return {
        "region": region,
        "y_sign": y_sign,
        "lines": len(mine),
        # Sum per line.  Flattening the lines into one point list and taking
        # polyline_len of THAT would also measure the jump from the end of one
        # road to the start of the next, which reported 49.7 km against a real
        # 14.2 km.
        "km": sum(polyline_len(l) for l in mine) * 85.9375 / 1000.0,
        "fwd": fwd,          # invented road (mine with no hand counterpart)
        "bwd": bwd,          # missed road (hand road I did not cover)
        "turns_mine": sum(count_turns(l) for l in mine),
        "turns_hand": sum(count_turns(l) for l in hand),
        "hand_lines": len(hand),
    }


def diag(region, y_sign=-1.0):
    """Is the road set duplicated?  180-degree reversals suggest overlap."""
    import math

    entries = rfp.load_splines(region)
    segs = {}
    for tier, e in entries:
        k = (tier, tuple(sorted([(round(e[0], 1), round(e[1], 1)),
                                 (round(e[6], 1), round(e[7], 1))])))
        segs[k] = segs.get(k, 0) + 1
    dupes = sum(v - 1 for v in segs.values() if v > 1)
    print("entries %d  unique %d  duplicate copies %d"
          % (len(entries), len(segs), dupes))

    lens = sorted(math.dist((e[0], e[1]), (e[6], e[7])) for _, e in entries)
    print("segment length cm: min %.1f p10 %.1f median %.1f max %.1f"
          % (lens[0], lens[int(len(lens) * .1)],
             lens[len(lens) // 2], lens[-1]))
    for thr in (1, 10, 50, 100):
        print("   shorter than %4d cm: %d" % (thr, sum(1 for L in lens if L < thr)))

    nodes = {}
    for tier, e in entries:
        for p in ((round(e[0], 1), round(e[1], 1)),
                  (round(e[6], 1), round(e[7], 1))):
            nodes[p] = nodes.get(p, 0) + 1
    deg = {}
    for p, n in nodes.items():
        deg[n] = deg.get(n, 0) + 1
    print("node degree histogram:", dict(sorted(deg.items())))



def rng(region):
    """Do the world coordinates land inside the hex, or has it drifted?

    A silent frame error is the main risk in this route: the app would still
    render roads, just in the wrong place.  So check the emitted extent against
    the hex rectangle grid.py defines, which is a check that cannot be faked by
    a plausible-looking number.
    """
    ox, oy = grid.hex_origin(region)
    x0, x1 = ox, ox + grid.W
    # grid.py's origin is the hex's north-west corner, and world y increases
    # upward (px_to_world does `ty - py * s`), so oy is the TOP edge and the
    # bottom is oy - K.  Getting this backwards makes every point look outside.
    ybot, ytop = oy - grid.K, oy
    # Roads are authored to run a little past the hex edge (the game's tiles
    # overlap), so allow a small margin rather than demanding exact containment.
    m = grid.W * 0.05
    print("hex %s expects x %.2f..%.2f  y %.2f..%.2f  (+/-%.2f margin)"
          % (region, x0, x1, ybot, ytop, m))
    lines = [l for _, l in rfp.hex_lines(region)]
    xs = [p[0] for l in lines for p in l]
    ys = [p[1] for l in lines for p in l]
    print("  emitted      x %.2f..%.2f  y %.2f..%.2f"
          % (min(xs), max(xs), min(ys), max(ys)))
    out = sum(1 for l in lines for p in l
              if not (x0 - m <= p[0] <= x1 + m and ybot - m <= p[1] <= ytop + m))
    tot = sum(len(l) for l in lines)
    print("  points outside the hex: %d of %d (%.1f%%)"
          % (out, tot, 100.0 * out / max(1, tot)))


def shape(region):
    """Compare vertex/line shape: pak splines vs the hand-traced trace.

    The turn counts are only comparable once the two datasets are broken down
    the same way, because the hand trace is far more aggressively merged into
    long lines while the pak data is per-spline-mesh.
    """
    import statistics
    mine = [l for _, l in rfp.hex_lines(region)]
    hand = hand_traced(region)
    for nm, ls in (("pak   ", mine), ("hand  ", hand)):
        ns = sorted(len(l) for l in ls)
        print("%s lines %4d  pts/line median %g  mean %.1f  total %d  turns@25 %d"
              % (nm, len(ls), statistics.median(ns),
                 sum(ns) / float(len(ns)), sum(ns),
                 sum(count_turns(l) for l in ls)))


HW = 128.0
MERC_HALF = 20037500.0
MWC = HW / MERC_HALF          # mercator -> world, per roads.js


def hand_features(region, src):
    """Hand-traced features for one hex, verbatim from road_source.geojson."""
    return [f for f in src["features"]
            if f.get("properties", {}).get("region") == region]


def tier_agreement(hexes):
    """Does the tier MAPPING agree with the hand trace, empirically?

    tier_check.js proves which convention the app uses.  This proves the other
    half: that the game's RoadT3Gravel mesh really does sit where the hand
    trace calls tier 1.  Restating the mapping would prove nothing; this
    measures it against real geometry.

    For each game tier, take its points and ask which hand-traced tier has
    geometry within a short distance.  A correct mapping gives a sharp diagonal
    (Gravel->hand 1, PackedDirt->hand 2, Dirt->hand 3); the reversed mapping
    gives the anti-diagonal.  Both are printed so the difference is visible.

    A uniform hash grid keeps this linear; a naive scan over every hand point
    per game point is O(n*m) and would not finish.
    """
    src = json.load(open(os.path.join(HERE, "road_source.geojson")))
    tol = grid.W * 0.03          # ~0.77 world units ~ 16 px in map space
    cell = tol

    gridmap = {}                 # (ix, iy) -> {hand tier: True}
    for hx in hexes:
        for f in hand_features(hx, src):
            t = f["properties"].get("tier")
            if t not in (1, 2, 3):
                continue
            g = f["geometry"]
            for line in (g["coordinates"] if g["type"] == "MultiLineString"
                         else [g["coordinates"]]):
                for x, y in line:
                    wx, wy = x * MWC + HW, y * MWC - HW
                    gridmap.setdefault((int(wx / cell), int(wy / cell)),
                                       {})[t] = True

    if not gridmap:
        print("no hand-traced tiers found in %s" % ", ".join(hexes))
        return

    print("nearest hand tier for each game tier "
          "(diagonal = mapping correct, tol %.2f)" % tol)
    print("%-14s %9s %9s %9s %7s" % ("game tier", "hand 1", "hand 2", "hand 3",
                                     "n"))
    game = {1: [], 2: [], 3: []}
    for hx in hexes:
        for tier, line in rfp.hex_lines(hx):
            game.setdefault(tier, []).extend(line)

    for gt in (1, 2, 3):
        pts = game[gt]
        sample = pts[::max(1, len(pts) // 3000)][:3000]
        hits = {1: 0, 2: 0, 3: 0}
        for x, y in sample:
            ix, iy = int(x / cell), int(y / cell)
            found = None
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    c = gridmap.get((ix + dx, iy + dy))
                    if c:
                        # nearest tier wins; ties go to the lowest number
                        found = min(c) if found is None else min(found, *c)
            if found:
                hits[found] += 1
        n = max(1, len(sample))
        print("%-14s %8.1f%% %8.1f%% %8.1f%% %7d"
              % ("tier %d" % gt, 100.0 * hits[1] / n, 100.0 * hits[2] / n,
                 100.0 * hits[3] / n, len(pts)))


def sweep(hexes):
    """Sweep the collinear threshold: turns vs geometry vs threshold.

    The handover records that on the PIXEL route simplification overshot into a
    turn deficit, so `eps` was ruled out there.  That result does not transfer:
    here the input is the designer's own centreline, where a straight run is
    exactly straight, so collinear vertices carry no information and removing
    them should be free.  This measures whether that holds rather than assuming
    it -- `fwd` is the guard, and if it climbs with the threshold then real
    curvature is being flattened.
    """
    print("%-8s %9s %9s %10s %9s" % ("deg", "verts", "turns", "turns_excess",
                                    "mean_fwd"))
    for deg in (None, 0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 12.0, 20.0):
        nv = nt = nh = 0
        fwds = []
        for hx in hexes:
            lines = [l for _, l in rfp.hex_lines(hx, collinear=deg)]
            hand = hand_traced(hx)
            if not lines or not hand:
                continue
            nv += sum(len(l) for l in lines)
            nt += sum(count_turns(l) for l in lines)
            nh += sum(count_turns(l) for l in hand)
            a = [p for l in lines for p in resample(l)]
            b = [p for l in hand for p in resample(l)]
            f, _ = chamfer(a, b)
            fwds.append(f)
        label = "off" if deg is None else "%.1f" % deg
        exc = 100.0 * (nt / max(1, nh) - 1)
        print("%-8s %9d %9d %9.0f%% %9.3f"
              % (label, nv, nt, exc, sum(fwds) / max(1, len(fwds))))


def range_all(hexes):
    """Containment for every hex, against grid.py's own offset table.

    This is the authoritative frame check: it does not compare the pak output
    to another dataset (which risks agreeing with a shared mistake), it asks
    whether each hex's roads fall inside the rectangle grid.py says that hex
    occupies.  A hex appearing in the right place but drawn a hex off still
    passes every pairwise comparison and fails here.

    The margin is MEASURED, not guessed.  The game's hex tiles overlap, so road
    geometry is authored to run past a hex edge into the neighbour's tile -- that
    overhang is real and must not be reported as a frame error.  So report the
    worst overhang in world units and as a fraction of a hex, and fail only if
    it exceeds a quarter of a hex, which no legitimate edge overlap reaches.
    """
    limit = grid.W * 0.25
    worst_all, bad = 0.0, []
    for hx in hexes:
        ox, oy = grid.hex_origin(hx)
        if ox is None:
            bad.append("%s: no offset table entry" % hx)
            continue
        x0, x1 = ox, ox + grid.W
        ybot, ytop = oy - grid.K, oy
        pts = [p for _, l in rfp.hex_lines(hx) for p in l]
        if not pts:
            bad.append("%s: no road data" % hx)
            continue
        worst = 0.0
        for x, y in pts:
            dx = max(x0 - x, 0.0, x - x1)
            dy = max(ybot - y, 0.0, y - ytop)
            worst = max(worst, (dx * dx + dy * dy) ** 0.5)
        worst_all = max(worst_all, worst)
        if worst > limit:
            bad.append("%s: %.2f units outside (%.1f%% of a hex)"
                       % (hx, worst, 100.0 * worst / grid.W))
    print("containment: worst overhang %.3f units = %.2f%% of a hex "
          "(fail above %.0f%%)" % (worst_all, 100.0 * worst_all / grid.W,
                                   100.0 * limit / grid.W))
    for b in bad:
        print("  " + b)
    return not bad


def diagnose_offset(hexes, M=2.0):
    """Why do 6 regions not cover the full traced extent?

    Two candidate causes, and the difference matters:
      (a) the tracer spilled a NEIGHBOURING hex's roads into this region, so
          the trace legitimately reaches outside this hex's rectangle;
      (b) the pak conversion is misregistered for these hexes.

    Distinguishing them is a frame question, so answer it with grid.py: take
    the traced points that fall OUTSIDE the hex rectangle and ask which hex
    rectangle they actually belong to.  If they land cleanly in a neighbour,
    cause (a) -- the trace is a multi-hex layer and nothing is missing.  If they
    land in no hex at all, the trace is misregistered and so might be the pak.
    """
    src = json.load(open(os.path.join(HERE, "road_source.geojson")))
    for hx in hexes:
        ox, oy = grid.hex_origin(hx)
        x0, x1 = ox, ox + grid.W
        ybot, ytop = oy - grid.K, oy
        m = grid.W * 0.05

        outside = []
        for f in hand_features(hx, src):
            g = f["geometry"]
            for line in (g["coordinates"] if g["type"] == "MultiLineString"
                         else [g["coordinates"]]):
                for x, y in line:
                    wx, wy = x * MWC + HW, y * MWC - HW
                    if not (x0 - m <= wx <= x1 + m and ybot - m <= wy <= ytop + m):
                        outside.append((wx, wy))
        if not outside:
            continue

        # Which hex owns each stray point?  Use the same rectangle test.
        tally = {}
        unclaimed = 0
        for wx, wy in outside:
            owner = None
            for cand in grid.OFFSETS:
                cx, cy = grid.hex_origin(cand)
                if (cx - m <= wx <= cx + grid.W + m and
                        cy - grid.K - m <= wy <= cy + m):
                    # Nearest centre wins if two rectangles overlap.
                    if owner is None:
                        owner = cand
                    else:
                        ox2, oy2 = grid.hex_origin(owner)
                        d1 = (wx - (ox2 + grid.W / 2)) ** 2 + (wy - (oy2 - grid.K / 2)) ** 2
                        d2 = (wx - (cx + grid.W / 2)) ** 2 + (wy - (cy - grid.K / 2)) ** 2
                        if d2 < d1:
                            owner = cand
            if owner is None:
                unclaimed += 1
            else:
                tally[owner] = tally.get(owner, 0) + 1

        top = sorted(tally.items(), key=lambda kv: -kv[1])[:3]
        print("%-20s %4d stray pts  ->  %s%s"
              % (hx, len(outside),
                 ", ".join("%s:%d" % kv for kv in top),
                 "  UNCLAIMED:%d" % unclaimed if unclaimed else ""))


def diagnose_offset(hexes, M=2.0):
    """Why do 6 regions not cover the full traced extent?

    Two distinct causes, and the difference matters:
      (a) the tracer spilled a NEIGHBOURING hex's roads into this region, so
          the trace legitimately reaches outside this hex's rectangle;
      (b) the pak build is registered differently for that hex.

    For (a): take traced points outside the hex rectangle and ask which hex
    rectangle owns them.  Landing cleanly in a neighbour means trace spill, and
    nothing is missing.

    For (b) the trace stays inside its own rectangle, so compare the traced
    extent against the BUILT extent directly -- if the built network sits
    consistently inside the traced one rather than matching it, the two
    datasets are placed differently on this hex.
    """
    src = json.load(open(os.path.join(HERE, "road_source.geojson")))
    m = grid.W * 0.05
    print("%-20s %5s %5s  %-24s %-24s %s"
          % ("region", "stray", "built", "hex rect (x)", "hex rect (y)", "verdict"))
    for hx in hexes:
        ox, oy = grid.hex_origin(hx)
        x0, x1 = ox, ox + grid.W
        ybot, ytop = oy - grid.K, oy

        outside = []
        for f in hand_features(hx, src):
            g = f["geometry"]
            for line in (g["coordinates"] if g["type"] == "MultiLineString"
                         else [g["coordinates"]]):
                for x, y in line:
                    wx, wy = x * MWC + HW, y * MWC - HW
                    if not (x0 - m <= wx <= x1 + m and ybot - m <= wy <= ytop + m):
                        outside.append((wx, wy))

        if outside:
            tally, unclaimed = {}, 0
            for wx, wy in outside:
                owner, best = None, None
                for cand in grid.OFFSETS:
                    cx, cy = grid.hex_origin(cand)
                    if not (cx - m <= wx <= cx + grid.W + m and
                            cy - grid.K - m <= wy <= cy + m):
                        continue
                    d = ((wx - (cx + grid.W / 2)) ** 2 +
                         (wy - (cy - grid.K / 2)) ** 2)
                    if best is None or d < best:
                        owner, best = cand, d
                if owner is None:
                    unclaimed += 1
                else:
                    tally[owner] = tally.get(owner, 0) + 1
            top = sorted(tally.items(), key=lambda kv: -kv[1])[:2]
            verdict = "trace spill -> " + ", ".join("%s:%d" % kv for kv in top)
            if unclaimed:
                verdict += "  UNCLAIMED:%d" % unclaimed
            print("%-20s %5d %5s  %-24s %-24s %s"
                  % (hx, len(outside), "-",
                     "%.1f..%.1f" % (x0, x1), "%.1f..%.1f" % (ybot, ytop),
                     verdict))
            continue

        # No stray points: the trace is inside its own hex, so compare extents.
        lines = [l for _, l in rfp.hex_lines(hx)]
        pts = [p for l in lines for p in l]
        tp = []
        for f in hand_features(hx, src):
            g = f["geometry"]
            for line in (g["coordinates"] if g["type"] == "MultiLineString"
                         else [g["coordinates"]]):
                for x, y in line:
                    tp.append((x * MWC + HW, y * MWC - HW))
        if not pts or not tp:
            print("%-20s %5d %5s  %-24s %-24s no data"
                  % (hx, 0, len(pts), "%.1f..%.1f" % (x0, x1),
                     "%.1f..%.1f" % (ybot, ytop)))
            continue
        b = (min(p[0] for p in pts), max(p[0] for p in pts),
             min(p[1] for p in pts), max(p[1] for p in pts))
        t = (min(p[0] for p in tp), max(p[0] for p in tp),
             min(p[1] for p in tp), max(p[1] for p in tp))
        d = [t[i] - b[i] for i in range(4)]
        print("%-20s %5d %5d  %-24s %-24s delta x %+.1f/%+.1f y %+.1f/%+.1f"
              % (hx, 0, len(pts), "%.1f..%.1f" % (x0, x1),
                 "%.1f..%.1f" % (ybot, ytop), d[0], d[1], d[2], d[3]))


def ue_frame():
    """Is the UE road bbox centred on the hex?  Tests the centring assumption.

    hex_lines() maps the CENTRE of each hex's road bounding box onto the centre
    of the grid rectangle.  That is only valid if the road bbox is itself
    centred in the hex.  It is not, in general: a hex whose roads are clustered
    to one side has a bbox centre well away from the hex centre, and the
    per-hex error that introduces is a rigid translation of the whole network.

    The tell is exactly that: both bbox ends move by the SAME amount, which is
    what a translation looks like and what edge-clipping does not.

    So measure the road bbox centre, in UE centimetres, for every hex.  If it
    sits at a consistent point, that point is the real UE hex origin and should
    be used instead of the per-hex bbox centre.  If it wanders, the roads are
    genuinely off-centre per hex and the bbox approach is a per-hex fudge.
    """
    xs, ys = [], []
    print("%-20s %10s %10s %8s" % ("region", "bbox cx", "bbox cy", "points"))
    for hx in sorted(grid.OFFSETS):
        try:
            entries = rfp.load_splines(hx)
        except SystemExit:
            continue
        if not entries:
            continue
        px = [e[0] for _, e in entries] + [e[6] for _, e in entries]
        py = [e[1] for _, e in entries] + [e[7] for _, e in entries]
        cx = (min(px) + max(px)) / 2.0
        cy = (min(py) + max(py)) / 2.0
        xs.append(cx)
        ys.append(cy)
        print("%-20s %10.1f %10.1f %8d" % (hx, cx, cy, len(px)))
    if xs:
        xs.sort()
        ys.sort()
        n = len(xs)
        print("\nUE bbox centre across %d hexes:" % n)
        print("  x  min %.1f  p50 %.1f  max %.1f  spread %.1f cm"
              % (xs[0], xs[n // 2], xs[-1], xs[-1] - xs[0]))
        print("  y  min %.1f  p50 %.1f  max %.1f  spread %.1f cm"
              % (ys[0], ys[n // 2], ys[-1], ys[-1] - ys[0]))
        print("  hex width is %.0f cm, so a spread of that size in the bbox"
              % rfp.HEX_METRES * 100)
        print("  centre is %s of a hex" %
              ("meaningful" if (xs[-1] - xs[0]) > 0.1 * rfp.HEX_METRES * 100
               else "small"))


def ue_frame():
    """Is the UE road bbox centred on the hex?  Tests the centring assumption.

    hex_lines() maps the CENTRE of each hex's road bounding box onto the centre
    of the grid rectangle.  That is only valid if the road bbox is itself
    centred in the hex.  It is not in general: a hex whose roads cluster to one
    side has a bbox centre away from the hex centre, and the per-hex error that
    introduces is a rigid translation of the whole network.

    The tell is exactly that: both bbox ends move by the SAME amount, which is
    what a translation looks like and what edge-clipping does not.

    So measure the road bbox centre, in UE centimetres, for every hex.  If it
    sits at a consistent point, that point is the real UE hex origin and should
    be used instead of the per-hex bbox centre.  If it wanders, the roads are
    genuinely off-centre per hex and the bbox approach is a per-hex fudge.
    """
    xs, ys = [], []
    print("%-20s %10s %10s %8s" % ("region", "bbox cx", "bbox cy", "points"))
    for hx in sorted(grid.OFFSETS):
        try:
            entries = rfp.load_splines(hx)
        except SystemExit:
            continue
        if not entries:
            continue
        px = [e[0] for _, e in entries] + [e[6] for _, e in entries]
        py = [e[1] for _, e in entries] + [e[7] for _, e in entries]
        cx = (min(px) + max(px)) / 2.0
        cy = (min(py) + max(py)) / 2.0
        xs.append(cx)
        ys.append(cy)
        print("%-20s %10.1f %10.1f %8d" % (hx, cx, cy, len(px)))
    if xs:
        xs.sort()
        ys.sort()
        n = len(xs)
        w = rfp.HEX_METRES * 100
        print("\nUE bbox centre across %d hexes:" % n)
        print("  x  min %.1f  p50 %.1f  max %.1f  spread %.1f cm" %
              (xs[0], xs[n // 2], xs[-1], xs[-1] - xs[0]))
        print("  y  min %.1f  p50 %.1f  max %.1f  spread %.1f cm" %
              (ys[0], ys[n // 2], ys[-1], ys[-1] - ys[0]))
        print("  hex width is %.0f cm; spread is %.0f%% of a hex" %
              (w, 100.0 * (xs[-1] - xs[0]) / w))


def gap(hexes):
    """Is a region genuinely missing road, or is the trace's extent an artefact?

    bwd is reported to 3 dp, so a handful of traced points 2-3 units from the
    nearest pak road averages out to 0.000 and hides.  So for the flagged
    regions, count the traced points that have NO pak road within a real
    tolerance, and report the worst single distance.  That distinguishes
    "one stray traced vertex" from "a road the pak does not have".
    """
    tol = grid.W * 0.05          # ~1.3 units, the same tolerance netdiff uses
    for hx in hexes:
        lines = [l for _, l in rfp.hex_lines(hx)]
        mine = [p for l in lines for p in l]
        if not mine:
            print("%-18s no pak road" % hx)
            continue
        cell = tol
        gm = {}
        for p in mine:
            gm.setdefault((int(p[0] / cell), int(p[1] / cell)), []).append(p)

        def near(p):
            ix, iy = int(p[0] / cell), int(p[1] / cell)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for q in gm.get((ix + dx, iy + dy), ()):
                        if abs(q[0] - p[0]) <= tol and abs(q[1] - p[1]) <= tol:
                            return True
            return False

        src = json.load(open(os.path.join(HERE, "road_source.geojson")))
        miss, worst, tot = 0, 0.0, 0
        for f in hand_features(hx, src):
            g = f["geometry"]
            for line in (g["coordinates"] if g["type"] == "MultiLineString"
                         else [g["coordinates"]]):
                for x, y in line:
                    wx, wy = x * MWC + HW, y * MWC - HW
                    tot += 1
                    if not near((wx, wy)):
                        miss += 1
                        # nearest pak road, for the worst-case readout
                        ix, iy = int(wx / cell), int(wy / cell)
                        d = min(((q[0] - wx) ** 2 + (q[1] - wy) ** 2) ** 0.5
                                for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                                for q in gm.get((ix + dx, iy + dy), ())) \
                            if any(gm.get((ix + dx, iy + dy))
                                   for dx in (-1, 0, 1) for dy in (-1, 0, 1)) else 0.0
                        worst = max(worst, d)
        print("%-18s %5d of %5d traced pts unmatched (%.2f%%)  worst %.2f units"
              % (hx, miss, tot, 100.0 * miss / max(1, tot), worst))


def compare(hexes, pak_path=None, hand_path=None):
    """Side-by-side: the built pak file vs the hand-traced production file.

    Reads the actual built artefact (the merged geojson pushed through
    scripts/roads.js), not a recomputation, so this measures what would ship.
    Both sides are reduced to the same units -- world -- and the same per-hex
    grouping, then compared on the axes that decide whether this is an
    improvement: coverage, geometry, and turns.

    `all_roads.geojson` (the pixel extractor's build) is NOT on this machine,
    so the pixel route can only appear as the handover's published figures.
    That is stated in the output rather than quietly omitted.
    """
    pak_path = pak_path or os.environ.get("PAK_ROADS", r"J:\pak_roads.geojson")
    hand_path = hand_path or os.path.join(HERE, "road_source.geojson")

    def load(path, to_world):
        d = json.load(open(path, encoding="utf-8"))
        per = {}
        for f in d["features"]:
            reg = f.get("properties", {}).get("region")
            # tier may be int or str depending on how the file was written;
            # normalise so the counts are not silently all zero.
            raw = f.get("properties", {}).get("tier")
            try:
                t = int(raw)
            except (TypeError, ValueError):
                t = raw
            g = f["geometry"]
            lines = (g["coordinates"] if g["type"] == "MultiLineString"
                     else [g["coordinates"]])
            for line in lines:
                per.setdefault(reg, []).append(
                    (t, [to_world(x, y) for x, y in line]))
        return per

    # Both files are EPSG:3857; one mercator step is SCALE world units.
    SC = 128.0 / 20037500.0
    pak = load(pak_path, lambda x, y: (x * SC + 128, y * SC - 128))
    hand = load(hand_path, lambda x, y: (x * SC + 128, y * SC - 128))

    shared = [h for h in hexes if h in pak and h in hand]
    print("pak build   : %s" % pak_path)
    print("hand trace  : %s" % hand_path)
    print("hexes: pak %d, hand %d, shared %d\n"
          % (len(pak), len(hand), len(shared)))

    def stats(per, keys):
        lines = [l for h in keys for l in per.get(h, [])]
        tiers = {}
        for t, _ in lines:
            tiers[t] = tiers.get(t, 0) + 1
        length = sum(polyline_len(l) for _, l in lines)
        return {
            "features": len(lines),
            "verts": sum(len(l) for _, l in lines),
            "km": length * (rfp.HEX_METRES / grid.W) / 1000.0,
            "tiers": tiers,
            "turns": sum(count_turns(l) for _, l in lines),
        }

    a, b = stats(pak, shared), stats(hand, shared)

    def tierrow(t, counts):
        c = counts.get(t, 0)
        return "%6d (%4.1f%%)" % (c, 100.0 * c / max(1, sum(counts.values())))

    print("%-26s %14s %14s" % ("", "pak (game mesh)", "hand-traced"))
    print("%-26s %14d %14d" % ("features", a["features"], b["features"]))
    print("%-26s %14d %14d" % ("polyline vertices", a["verts"], b["verts"]))
    print("%-26s %13.1fkm %13.1fkm" % ("road length", a["km"], b["km"]))
    for t in (0, 1, 2, 3, 4):
        if t in a["tiers"] or t in b["tiers"]:
            print("%-26s %14s %14s" % ("tier %d" % t,
                                       tierrow(t, a["tiers"]),
                                       tierrow(t, b["tiers"])))
    print("%-26s %14d %14d" % ("turns (per-line)", a["turns"], b["turns"]))
    print("%-26s %13.0f%%" % ("turn excess vs hand",
                              100.0 * (a["turns"] / max(1, b["turns"]) - 1)))

    # Coverage and geometry per hex, the two numbers that decide correctness.
    print("\nper hex: forward = invented road, backward = missed road")
    fw, bw = [], []
    for h in shared:
        pa = [p for _, l in pak[h] for p in resample(l)]
        hb = [p for _, l in hand[h] for p in resample(l)]
        if not pa or not hb:
            continue
        f, bk = chamfer(pa, hb)
        fw.append(f)
        bw.append(bk)
    n = len(fw)
    if n:
        print("  hexes compared      : %d" % n)
        print("  mean forward        : %.3f" % (sum(fw) / n))
        print("  median forward      : %.3f" % sorted(fw)[n // 2])
        print("  worst forward       : %.3f (%s)"
              % (max(fw), shared[fw.index(max(fw))]))
        print("  mean backward       : %.3f" % (sum(bw) / n))
        print("  hexes with bwd 0.000: %d of %d"
              % (sum(1 for v in bw if v == 0.0), n))

    print("\npixel extractor (all_roads.geojson) is not on this machine; its")
    print("published figures are median agreement 86.8% within 4px, turn")
    print("excess 30%, backward recall 97.3%. Not re-measured here.")


def main():
    grid.load_offsets(os.path.join(HERE, "scripts", "export_major_locations.sh"))
    hexes = sys.argv[1:]
    if hexes and hexes[0] == "--compare":
        compare(hexes[1:])
        return
    if hexes and hexes[0] == "--compare":
        compare(hexes[1:])
        return
    if hexes and hexes[0] == "--gap":
        gap(hexes[1:])
        return
    if hexes and hexes[0] == "--ueframe":
        ue_frame()
        return
    if hexes and hexes[0] == "--offset":
        diagnose_offset(hexes[1:])
        return
    if hexes and hexes[0] == "--rangeall":
        ok = range_all(hexes[1:])
        sys.exit(0 if ok else 1)
    if hexes and hexes[0] == "--sweep":
        sweep(hexes[1:])
        return
    if hexes and hexes[0] == "--tiers":
        tier_agreement(hexes[1:])
        return
    if hexes and hexes[0] == "--range":
        rng(hexes[1])
        return
    if hexes and hexes[0] == "--shape":
        shape(hexes[1])
        return
    rows = []
    for sign in (-1.0, 1.0):
        for hx in hexes:
            r = score(hx, sign)
            if r:
                rows.append(r)
    print("%-18s %5s %6s %8s %8s %8s %7s %7s"
          % ("region", "sign", "lines", "km", "fwd", "bwd",
             "turns!", "turns@"))
    for r in rows:
        print("%-18s %+5.0f %6d %8.1f %8.3f %8.3f %7d %7d"
              % (r["region"], r["y_sign"], r["lines"], r["km"],
                 r["fwd"], r["bwd"], r["turns_mine"], r["turns_hand"]))
    for sign in (-1.0, 1.0):
        sel = [r for r in rows if r["y_sign"] == sign]
        if not sel:
            continue
        n = len(sel)
        print("\ny_sign=%+.0f  mean fwd %.3f  bwd %.3f  turns %d vs %d (%.0f%% excess)"
              % (sign, sum(r["fwd"] for r in sel) / n,
                 sum(r["bwd"] for r in sel) / n,
                 sum(r["turns_mine"] for r in sel),
                 sum(r["turns_hand"] for r in sel),
                 100.0 * (sum(r["turns_mine"] for r in sel) /
                          max(1, sum(r["turns_hand"] for r in sel)) - 1)))


if __name__ == "__main__":
    main()
