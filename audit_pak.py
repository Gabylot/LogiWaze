"""
Per-line audit of the pak-derived road network against the hand trace.

WHY THIS EXISTS
---------------
The headline numbers (micro F1, avg turns/route) aggregate a whole hex into one
value, which hides the failure mode that actually matters for routing: a hex can
average 94% agreement while carrying one spline that leaves the road surface and
crosses open water.  A router will happily path a vehicle down that spline, so
the per-LINE distribution is the number that decides promotion, not the mean.

Only ground the hand trace actually covers is scored
----------------------------------------------------
Distance-to-hand conflates two very different situations: a road that is
MISPLACED, and a road in a part of the hex the human tracer never drew.  The
second is not a defect.  Scoring it as one made TheFingers look like a 60% hex
when it is 85% on the ground that was actually traced.  So the hand mask is
dilated (COVER_DILATE_PX) and only pak pixels inside that region are scored.
The uncovered fraction is printed alongside, because a hex where the tracer
covered almost nothing cannot support a strong claim either way.

Tail vs misaligned
------------------
A line that is a correct road with one dangling end is a much smaller problem
than a line that is offset along its whole length, and they need different
fixes.  A line is classified by its LONGEST CONTIGUOUS RUN of vertices sitting
on the hand trace:

    TAIL        a long run is correctly placed; the rest dangles
    MISALIGNED  almost no part of it ever touches the hand trace

Splitting the line in half does not work: a short tail can be most of the
vertex count, so a good road with a stray end gets reported as two bad halves.

WHAT THIS DOES NOT MEASURE
-------------------------
Tier correctness is NOT checked here and is not a plausible failure mode:
roads_from_pak.classify() takes the tier from the game mesh NAME via an exact
match, so a mis-tiered line would need a name the table does not contain.  Use
--meshes to print the mesh inventory and confirm that.

    python3 audit_pak.py                     # all hexes with a hand trace
    python3 audit_pak.py AcrithiaHex ...     # just these
    python3 audit_pak.py --min-line 0.5      # only lines scoring below this
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage as ndi

import grid
import render_overlay as ro

HERE = os.path.dirname(os.path.abspath(__file__))
HAND = os.path.join(HERE, "road_source.geojson")

# A pixel counts as "on the hand trace" within this distance.  4px at 2048 map
# resolution is ~8px at the 1024 render width, comfortably inside the width of
# the painted road surface, so it measures "on the road" rather than "on the
# same pixel as the tracer's stroke".
ON_ROAD_PX = 4.0
# How far outside its own strokes the hand trace is still treated as covered.
# Generous, so a road just past the edge of the traced area is not scored as a
# miss.  This is a judgment call; it moves absolute percentages, not the
# tail/misaligned split, which is structural.
COVER_DILATE_PX = 25
# Below this fraction of on-road pixels a line is reported as suspect.
BAD_LINE = 0.5
# Lines with fewer comparable pixels than this are too small to judge.
MIN_PX = 200
# A line needs at least this many consecutive on-road vertices to count as a
# road-with-a-tail rather than a line that never touches the trace at all.
MIN_RUN_VTX = 8


def load_hand_by_region():
    d = json.load(open(HAND, encoding="utf-8"))
    out = {}
    for f in d["features"]:
        r = f.get("properties", {}).get("region")
        if r:
            out.setdefault(r, []).append(f)
    return out


def mesh_inventory(region):
    """Spline mesh names in the pak JSON, with the tier classify() assigns.

    Printed so a reader can confirm no line is tiered by a name the table does
    not cover -- the one way tier could silently be wrong.
    """
    import roads_from_pak as rfp
    path = os.path.join(rfp.PAK_JSON, region + ".json")
    if not os.path.isfile(path):
        return None
    sp = json.load(open(path, encoding="utf-8")).get("splines", {})
    return sorted((rfp.classify(n), len(v), n) for n, v in sp.items())


def raster(lines, W, H, width=7):
    im = Image.new("1", (W, H), 0)
    d = ImageDraw.Draw(im)
    for _t, pts in lines:
        if len(pts) > 1:
            d.line([(x, y) for x, y in pts], fill=1, width=width)
    return np.array(im, bool)


def audit_hex(region, verbose_meshes=False):
    """Return a dict of per-tier and per-line results for one hex."""
    png = ro.find_png(region)
    if png is None:
        return None
    hand = ro.load_hand(region)
    pak = ro.load_pak(region) or []
    if not hand or not pak:
        return None

    W, H = Image.open(png).convert("RGB").size

    # load_hand/load_pak already return map pixels of the full image, so these
    # rasters are drawn 1:1.
    hm = raster(hand, W, H)
    # return_indices gives, for every pixel, the coordinates of the nearest
    # hand-trace pixel -- so the vector from that pixel to a pak vertex is the
    # SIGNED displacement.  An unsigned distance cannot tell "this line is
    # shifted 12px north" from "this line is wrong"; the sign can.
    dt_hand, idx = ndi.distance_transform_edt(~hm, return_indices=True)
    covered = ndi.binary_dilation(hm, iterations=COVER_DILATE_PX)

    tiers, lines = {}, []
    for t, pts in pak:
        if len(pts) < 2:
            continue
        m = raster([(t, pts)], W, H)
        if not m.any():
            continue
        tiers.setdefault(t, []).append(m)
        sel = m & covered
        if sel.sum() < MIN_PX:
            continue
        onroad = []
        for x, y in pts:
            ix, iy = int(round(x)), int(round(y))
            onroad.append(0 <= ix < W and 0 <= iy < H
                          and dt_hand[iy, ix] <= ON_ROAD_PX)
        best = cur = 0
        for b in onroad:
            cur = cur + 1 if b else 0
            best = max(best, cur)
        lines.append({
            "tier": t,
            "frac": float((dt_hand[sel] <= ON_ROAD_PX).mean()),
            "run": best, "npts": len(pts),
            "kind": "TAIL" if best >= MIN_RUN_VTX else "MISALIGNED",
            "bbox": (min(x for x, _ in pts), min(y for _, y in pts),
                     max(x for x, _ in pts), max(y for _, y in pts)),
            # Length in map pixels, and whether the line stays inside its own
            # hex.  A line running outside the hex bounds is a different
            # failure from one drawn in the wrong place inside them.
            "length": float(sum(
                ((pts[i + 1][0] - pts[i][0]) ** 2 +
                 (pts[i + 1][1] - pts[i][1]) ** 2) ** 0.5
                for i in range(len(pts) - 1))),
            "pts": pts,
            "_region": region,
        })

        # Signed displacement: for each comparable pixel, the vector from the
        # nearest hand-trace pixel to the pak pixel.  A line that is merely
        # OFFSET has a tight cluster around one non-zero vector; a line that is
        # genuinely wrong scatters in every direction.  The median is used so a
        # few wild vertices cannot drag it.
        ys, xs_ = np.nonzero(sel)
        if ys.size:
            dxs = (xs_ - idx[0][ys, xs_]).astype(float)
            dys = (ys - idx[1][ys, xs_]).astype(float)
            res_off = (float(np.median(dxs)), float(np.median(dys)))
            mag = float(np.hypot(*res_off))
            mean_mag = float(np.hypot(dxs, dys).mean())
            # Only meaningful when the line is CLOSE to the hand trace.  Far
            # away, the nearest hand pixel is whichever blob happens to be
            # least far, so every vector points the same way at a huge length
            # and "concentration" is 1.0 for a line that is simply elsewhere.
            # Lines beyond this are reported as NaN rather than as a clean
            # displacement, which would be a fabricated number.
            conc = (mag / mean_mag) if 0 < mean_mag <= 30.0 else float("nan")
        else:
            res_off, conc = (float("nan"), float("nan"))
        lines[-1]["off"] = res_off
        lines[-1]["conc"] = conc

    all_pak = np.zeros((H, W), bool)
    for v in tiers.values():
        for m in v:
            all_pak |= m
    sel = all_pak & covered

    res = {
        "region": region,
        "n_hand": len(hand), "n_pak": len(pak),
        "comparable_frac": (float(sel.sum()) / float(all_pak.sum())
                            if all_pak.any() else 0.0),
        "overall": (float((dt_hand[sel] <= ON_ROAD_PX).mean())
                    if sel.any() else float("nan")),
        "tiers": {}, "lines": lines,
    }
    for t, v in sorted(tiers.items()):
        m = np.zeros((H, W), bool)
        for mm in v:
            m |= mm
        s = m & covered
        res["tiers"][t] = {
            "comparable": int(s.sum()),
            "frac": (float((dt_hand[s] <= ON_ROAD_PX).mean())
                     if s.any() else float("nan")),
        }
    if verbose_meshes:
        res["meshes"] = mesh_inventory(region)
    return res


def mean_agreement(results, tol):
    """Mean on-road agreement over all audited pixels, at an arbitrary tolerance.

    The tolerance is baked into audit_hex's per-line fractions, so this
    recomputes from the stored line points rather than reusing them -- otherwise
    the sweep would be circular and would just reprint the same number.
    """
    num = den = 0.0
    for res in results:
        hand = ro.load_hand(res["region"])
        if not hand:
            continue
        png = ro.find_png(res["region"])
        if png is None:
            continue
        W, H = Image.open(png).convert("RGB").size
        hm = raster(hand, W, H)
        dt = ndi.distance_transform_edt(~hm)
        cov = ndi.binary_dilation(hm, iterations=COVER_DILATE_PX)
        for l in res["lines"]:
            m = raster([(l["tier"], l["pts"])], W, H)
            s = m & cov
            if not s.any():
                continue
            num += float((dt[s] <= tol).sum())
            den += float(s.sum())
    return num / den if den else float("nan")


def band_offsets(results, lo, hi, gate=40.0):
    """Signed displacement pooled over every line whose agreement is in [lo,hi).

    A per-LINE median cannot answer "is there one global calibration error",
    because each line is measured against a different part of the map.  This
    pools every comparable pixel from the band across all hexes and looks at the
    distribution of displacement vectors.  If one calibration constant is
    wrong, the pooled cloud is a tight off-centre blob; if the band is just
    authoring variance, it is a centred disc.

    Returns a dict of the pooled statistics plus a coarse 2D histogram, so the
    shape is visible rather than reduced to one number.
    """
    dxs, dys, per_hex = [], [], {}
    for res in results:
        hand = ro.load_hand(res["region"])
        if not hand:
            continue
        png = ro.find_png(res["region"])
        if png is None:
            continue
        W, H = Image.open(png).convert("RGB").size
        hm = raster(hand, W, H)
        dt, ind = ndi.distance_transform_edt(~hm, return_indices=True)
        cov = ndi.binary_dilation(hm, iterations=COVER_DILATE_PX)
        hx, hy = [], []
        for l in res["lines"]:
            if not (lo <= l["frac"] < hi):
                continue
            m = raster([(l["tier"], l["pts"])], W, H)
            s = m & cov
            if not s.any():
                continue
            ys, xs_ = np.nonzero(s)
            hx.append((xs_ - ind[0][ys, xs_]).astype(float))
            hy.append((ys - ind[1][ys, xs_]).astype(float))
        if not hx:
            continue
        hx, hy = np.concatenate(hx), np.concatenate(hy)
        # A line in this band is by definition within ~gate of the trace, but a
        # single stray vertex can still be far; drop those so they cannot
        # dominate the pooled median.
        keep = np.hypot(hx, hy) <= gate
        # A hex can contribute nothing if every one of its band lines had all
        # its comparable pixels beyond the gate.  Skip it rather than storing a
        # NaN median, which poisons the pooled per-hex statistics below.
        if keep.sum() < 50:
            continue
        dxs.append(hx[keep])
        dys.append(hy[keep])
        per_hex[res["region"]] = (float(np.median(hx[keep])),
                                  float(np.median(hy[keep])), int(keep.sum()))
    if not dxs:
        return None
    X, Y = np.concatenate(dxs), np.concatenate(dys)
    out = {
        "n": int(X.size), "hexes": len(per_hex),
        "med": (float(np.median(X)), float(np.median(Y))),
        "mean_abs": float(np.hypot(X, Y).mean()),
        "per_hex": per_hex,
    }
    # Coarse 16x16 histogram over +/-gate, as counts -- enough to see blob vs
    # disc without pulling in a plotting dependency.
    g = gate
    H2, _, _ = np.histogram2d(X, Y, bins=16,
                              range=[[-g, g], [-g, g]])
    out["hist"] = H2
    return out


def collect_band_vectors(results, lo, hi, gate=40.0):
    """Pooled (position, displacement) pairs for every line in an agreement band.

    Positions are in map pixels relative to the hex centre, so a transform
    fitted here is a transform of the hex's own frame -- which is what a
    rotation about a common centre means.  Gated to keep far-away pixels from
    dominating; a pixel beyond `gate` carries no usable direction information.
    """
    P, D, per = [], [], {}
    for res in results:
        hand = ro.load_hand(res["region"])
        if not hand:
            continue
        png = ro.find_png(res["region"])
        if png is None:
            continue
        W, H = Image.open(png).convert("RGB").size
        hm = raster(hand, W, H)
        _dt, ind = ndi.distance_transform_edt(~hm, return_indices=True)
        cov = ndi.binary_dilation(hm, iterations=COVER_DILATE_PX)
        px, py, dx, dy = [], [], [], []
        for l in res["lines"]:
            if not (lo <= l["frac"] < hi):
                continue
            m = raster([(l["tier"], l["pts"])], W, H)
            s = m & cov
            if not s.any():
                continue
            ys_, xs_ = np.nonzero(s)
            vx = (xs_ - ind[0][ys_, xs_]).astype(float)
            vy = (ys_ - ind[1][ys_, xs_]).astype(float)
            keep = np.hypot(vx, vy) <= gate
            if keep.sum() < 50:
                continue
            px.append(xs_[keep] - W / 2.0)
            py.append(ys_[keep] - H / 2.0)
            dx.append(vx[keep])
            dy.append(vy[keep])
        if not px:
            continue
        px = np.concatenate(px)
        py = np.concatenate(py)
        dx = np.concatenate(dx)
        dy = np.concatenate(dy)
        P.append(np.column_stack([px, py]))
        D.append(np.column_stack([dx, dy]))
        per[res["region"]] = len(dx)
    if not P:
        return None
    return np.concatenate(P), np.concatenate(D), per


def fit_transform(P, D):
    """Least-squares fit of displacement = A @ position + t.

    A general 2x2 affine is fitted rather than a rotation alone, because the
    point is to find out WHETHER one global transform explains the band at all.
    If a general affine cannot, then neither can a rotation, and the band is
    genuine per-hex authoring variance.  Returns (A, t, r2, residual_px).
    """
    n = P.shape[0]
    # Design matrix: [x y 1] -> two displacement components.
    X = np.column_stack([P[:, 0], P[:, 1], np.ones(n)])
    sol, *_ = np.linalg.lstsq(X, D, rcond=None)      # 3x2
    A = sol[:2].T                                      # 2x2
    t = sol[2]                                        # 2
    pred = X @ sol
    resid = D - pred
    ss_res = float((resid ** 2).sum())
    ss_tot = float(((D - D.mean(0)) ** 2).sum())
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")
    return A, t, r2, float(np.sqrt((resid ** 2).sum(axis=1).mean()))


def decompose(A):
    """Split a 2x2 into rotation, isotropic scale and shear.

    For a pure rotation+scale, c == -b and a == d.  The deviation of (b + c)
    from zero is the shear component, reported separately so a shear is not
    silently mislabelled as a rotation.
    """
    a, b = A[0, 0], A[0, 1]
    c, d = A[1, 0], A[1, 1]
    ang = np.degrees(np.arctan2(c, a))
    scale = np.sqrt(a * a + c * c)
    shear = (b + c) / 2.0
    return float(ang), float(scale), float(shear)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("hexes", nargs="*")
    ap.add_argument("--min-line", type=float, default=BAD_LINE,
                    help="report lines scoring below this (default %.2f)"
                         % BAD_LINE)
    ap.add_argument("--meshes", action="store_true",
                    help="also print the pak spline mesh inventory per hex")
    ap.add_argument("--worst", type=int, metavar="N", default=0,
                    help="instead of the usual report, list the N longest "
                         "misaligned lines with crop boxes for render_overlay")
    ap.add_argument("--offset", action="store_true",
                    help="report the SIGNED displacement of each line from the "
                         "hand trace.  Parallel-but-offset lines share one "
                         "sign, which points at a calibration error rather than "
                         "bad geometry")
    ap.add_argument("--sensitivity", action="store_true",
                    help="sweep the bad-line threshold and the on-road "
                         "tolerance, to show how much any headline percentage "
                         "depends on those two choices")
    ap.add_argument("--band", nargs=2, type=float, metavar=("LO", "HI"),
                    help="pooled signed-displacement analysis for lines whose "
                         "agreement lies in [LO,HI), e.g. --band 0.7 0.9")
    ap.add_argument("--fit", nargs=2, type=float, metavar=("LO", "HI"),
                    help="fit a single global affine transform to the "
                         "displacements of lines in [LO,HI), with a "
                         "leave-one-hex-out check that it is not overfitting")
    ap.add_argument("--ends", type=int, metavar="N", default=0,
                    help="for the N longest misaligned lines, report how far "
                         "each endpoint is from the hand trace -- a stranded "
                         "end is a different defect from a parallel line")
    ap.add_argument("--scale", action="store_true",
                    help="print every pixel threshold in METRES, because a "
                         "px figure on its own hides whether a disagreement is "
                         "a real defect or sub-road-width noise")
    a = ap.parse_args()
    grid.load_offsets(os.path.join(HERE, "scripts",
                                   "export_major_locations.sh"))

    results = []
    for r in (a.hexes or sorted(load_hand_by_region())):
        res = audit_hex(r, verbose_meshes=a.meshes)
        if res is None:
            print("%-22s skipped (no map, hand trace or pak data)" % r)
            continue
        results.append(res)
    if not results:
        raise SystemExit("no comparable hexes")

    if a.ends:
        # Do the bad lines start or end in open country?  A line whose END is
        # stranded is a different defect from a line that runs parallel to the
        # road for its whole length, and the fix differs: one is a spline that
        # needs trimming, the other is a mis-drawn road.
        rows = []
        for res in results:
            hand = ro.load_hand(res["region"])
            if not hand:
                continue
            png = ro.find_png(res["region"])
            if png is None:
                continue
            W, H = Image.open(png).convert("RGB").size
            hm = raster(hand, W, H)
            dt = ndi.distance_transform_edt(~hm)
            cov = ndi.binary_dilation(hm, iterations=COVER_DILATE_PX)
            for l in res["lines"]:
                if l["frac"] >= a.min_line or l["kind"] != "MISALIGNED":
                    continue
                pts = l["pts"]
                ends = []
                for tag, pt in (("start", pts[0]), ("end", pts[-1])):
                    ix, iy = int(round(pt[0])), int(round(pt[1]))
                    d = (float(dt[iy, ix])
                         if 0 <= ix < W and 0 <= iy < H else float("inf"))
                    ends.append((tag, pt[0], pt[1], d))
                rows.append((l["frac"], l["tier"], l["_region"], l["length"],
                             ends))
        rows.sort(key=lambda r: r[3], reverse=True)
        print("endpoints of the longest misaligned lines, distance to the "
              "hand trace in map px (and metres)")
        for f, t, reg, L, ends in rows[:a.ends]:
            s = "   ".join("%s(%4.0f,%4.0f) d=%s" %
                           (tag, x, y,
                            "%.0fpx/%.0fm" % (d, d * 1.074) if d != float("inf")
                            else "off-map")
                           for tag, x, y, d in ends)
            print("   %4.0fpx T%d %-20s  %s" % (L, t, reg, s))
        # Summary: what fraction of bad lines have a stranded end?
        stranded = 0
        tot = 0
        for f, t, reg, L, ends in rows:
            d = [e[3] for e in ends]
            tot += 1
            if max(d) > 40:
                stranded += 1
        print("\n   %d of %d bad lines have an endpoint >40px (~43m) from the "
              "hand trace" % (stranded, tot))
        return results

    if a.scale:
        # Every number so far has been in pixels, which invites reading a 4px
        # disagreement as a defect.  A hex is 2200 m across 2048 px, so 1 px is
        # ~1.07 m and a 4px tolerance is ~4.3 m -- roughly one road width.  On
        # that scale most of what the audit flags is not "the road is in the
        # wrong place", it is "the tracer and the game disagree by less than a
        # lane".  Printed explicitly so the thresholds are read in real units.
        import roads_from_pak as rfp
        m_per_px = rfp.HEX_METRES / 2048.0
        print("1 map px = %.2f m  (hex is %d m across 2048 px)\n"
              % (m_per_px, rfp.HEX_METRES))
        allines = [l for r in results for l in r["lines"]]
        total = sum(l["length"] for l in allines)
        print("share of road length by agreement, at the 4px (~%.1f m) "
              "tolerance:" % (4 * m_per_px))
        for lo, hi in ((0.9, 1.01), (0.7, 0.9), (0.5, 0.7), (0.3, 0.5),
                       (0.0, 0.3)):
            L = sum(l["length"] for l in allines if lo <= l["frac"] < hi)
            print("   %3.0f-%3.0f%%: %5.1f%% of length" % (100 * lo,
                                                           100 * min(1, hi),
                                                           100 * L / total))
        print("\nthe same bands at a 10 m tolerance (~%.1f px), which is the "
              "scale at which a" % (10 / m_per_px))
        print("disagreement stops being 'the road is somewhere else' and "
              "becomes 'lane placement':")
        for tol in (4.0, 8.0, 10 / m_per_px, 20 / m_per_px):
            f = mean_agreement(results, tol)
            print("   %5.1f m (%4.1f px): agreement %5.1f%%"
                  % (tol * m_per_px, tol, 100 * f))
        return results

    if a.fit:
        lo, hi = a.fit
        got = collect_band_vectors(results, lo, hi)
        if got is None:
            raise SystemExit("no vectors in band %.2f-%.2f" % (lo, hi))
        P, D, per = got
        print("band %.0f-%.0f%%: %d displacement vectors over %d hexes"
              % (100 * lo, 100 * hi, P.shape[0], len(per)))
        print("  raw displacement magnitude: mean=%.2f px  p90=%.2f px"
              % (np.hypot(D[:, 0], D[:, 1]).mean(),
                 np.percentile(np.hypot(D[:, 0], D[:, 1]), 90)))

        A, t, r2, res = fit_transform(P, D)
        ang, scale, shear = decompose(A)
        print("\n  global affine fit  d = A p + t")
        print("    A = [[%+.6f %+.6f] [%+.6f %+.6f]]"
              % (A[0, 0], A[0, 1], A[1, 0], A[1, 1]))
        print("    t = (%+.3f, %+.3f) px" % (t[0], t[1]))
        sv = np.linalg.svd(A, compute_uv=False)
        # 1e-3 was too tight to catch the real case: this fit has a singular
        # value ratio of 0.00114, which is 870:1 anisotropy -- numerically
        # rank 1, but it squeaked past a 1e-3 test.  1e-2 is a sane bar for
        # "this is actually two-dimensional".
        rank = int((sv > sv[0] * 1e-2).sum())
        print("    singular values %.4f, %.4f  (ratio %.5f)  rank %d"
              % (sv[0], sv[1], sv[1] / sv[0], rank))
        print("    R^2 = %.4f   residual %.2f px" % (r2, res))

        # A rank-deficient A collapses every displacement onto ONE axis, which
        # makes R^2 look excellent while the rotation/scale figures below are
        # meaningless -- decomposing a degenerate matrix yields confident-
        # looking garbage.  This fit reported a 44-degree rotation and 1.4x
        # scale from a matrix with determinant 0.004, which is nonsense dressed
        # as a parameter.  Never let a caller read a rotation off a projection.
        if rank < 2:
            print("\n    !! A is RANK %d -- a PROJECTION onto a single axis, not"
                  % rank)
            print("       an invertible transform.  rotation/scale/shear are")
            print("       UNDEFINED here and must not be applied to any data.")
            print("       A high R^2 in that case means only that the")
            print("       displacement is confined to one direction, which is")
            print("       what a line offset PERPENDICULAR to its own bearing")
            print("       looks like.  It is not evidence of a global")
            print("       calibration error.")
        else:
            print("    rotation %+.4f deg   scale %.5f   shear %+.6f"
                  % (ang, scale, shear))

        # A transform that explains the band would leave a residual far below
        # the original displacement magnitude.  R^2 alone can look healthy on
        # noisy data, so compare the two directly.
        raw = float(np.sqrt((D ** 2).sum(axis=1).mean()))
        print("    residual is %.0f%% of the raw displacement"
              % (100 * res / raw))
        if rank < 2:
            print("    (that ratio is NOT evidence of a fixable global error --"
                  " see the rank warning above)")

        # Leave-one-hex-out.  A transform fitted on 30 hexes and applied to a
        # 31st only helps if the error really is common.  If held-out hexes do
        # not improve, the fit is memorising the training set.
        print("\n  leave-one-hex-out (fit without a hex, apply to it):")
        tot_b = tot_a = 0.0
        helps = n_scored = 0
        for held in sorted(per):
            m = np.ones(P.shape[0], bool)
            off = 0
            for res in results:
                n = per.get(res["region"], 0)
                if res["region"] == held:
                    m[off:off + n] = False
                off += n
            if m.sum() < 500 or (~m).sum() < 50:
                continue
            Ah, th, _, _ = fit_transform(P[m], D[m])
            pred = (Ah @ P[~m].T).T + th
            n_h = int((~m).sum())
            # Accumulate SQUARED magnitudes, not per-fold rms.  Averaging rms
            # values and then taking a square root of the mean is not the rms
            # of the pooled set and produced a baseline (4.77px) below the
            # plain mean displacement (19.79px), which is impossible.
            tot_b += float((D[~m] ** 2).sum())
            tot_a += float(((D[~m] - pred) ** 2).sum())
            n_scored += n_h
            if ((D[~m] - pred) ** 2).sum() < (D[~m] ** 2).sum():
                helps += 1
        if n_scored:
            print("    held-out |d| rms %.2f -> %.2f px over %d vectors, "
                  "improved on %d hexes"
                  % ((tot_b / n_scored) ** 0.5,
                     (tot_a / n_scored) ** 0.5, n_scored, helps))
        return results

    if a.band:
        lo, hi = a.band
        r = band_offsets(results, lo, hi)
        if r is None:
            raise SystemExit("no lines in band %.2f-%.2f" % (lo, hi))
        print("band %.0f-%.0f%% agreement: %d px over %d hexes"
              % (100 * lo, 100 * hi, r["n"], r["hexes"]))
        print("  pooled median dx=%+.2f dy=%+.2f   mean |d|=%.2f px"
              % (r["med"][0], r["med"][1], r["mean_abs"]))
        # The decisive test.  A single wrong calibration constant displaces
        # EVERY hex the same way, so the per-hex medians would all agree with
        # each other.  Authoring variance gives per-hex medians scattered
        # around zero.  Comparing their spread against their magnitude says
        # which, without having to eyeball a histogram.
        hx = np.array([v[0] for v in r["per_hex"].values()])
        hy = np.array([v[1] for v in r["per_hex"].values()])
        m = np.hypot(hx, hy)
        print("  per-hex median offsets: |d| min=%.1f med=%.1f max=%.1f px"
              % (m.min(), np.median(m), m.max()))
        print("  mean resultant length R=%.3f  (0 = scattered, 1 = all identical)"
              % (m.mean() and float(np.hypot(hx.mean(), hy.mean())) / m.mean()))
        print("\n  displacement histogram, +/-40px, rows = dy, cols = dx")
        g = 40.0
        H2 = r["hist"]
        mx = H2.max()
        ramp = " .:-=+*#%@"
        for ri in range(H2.shape[0] - 1, -1, -1):
            row = H2[ri]
            line = "".join(ramp[min(9, int(9 * v / mx))] if v else " "
                           for v in row)
            dyv = -g + (ri + 0.5) * (2 * g / H2.shape[0])
            print("   |%s| %+5.1f" % (line, dyv))
        print("      dx %+.0f .. %+.0f" % (-g, g))
        worst = sorted(r["per_hex"].items(), key=lambda kv: -np.hypot(*kv[1][:2]))
        print("\n  hexes with the largest median offset:")
        for k, (dx, dy, n) in worst[:10]:
            print("     %+6.1f %+6.1f  |d|=%5.1f  n=%-7d %s"
                  % (dx, dy, np.hypot(dx, dy), n, k))
        return results

    if a.sensitivity:
        # Every headline number so far has been produced by two arbitrary
        # choices: ON_ROAD_PX (how close counts as "on the road") and the
        # bad-line threshold.  This sweeps both, plus reports the THRESHOLD-FREE
        # number, so it is visible which figures are properties of the data and
        # which are artifacts of a cutoff.
        allines = [l for r in results for l in r["lines"]]
        total = sum(l["length"] for l in allines)
        print("audited road length: %.0fpx over %d lines\n" % (total,
                                                              len(allines)))

        print("threshold-free: share of road length by agreement band")
        for lo, hi in ((0.9, 1.01), (0.7, 0.9), (0.5, 0.7), (0.3, 0.5),
                       (0.0, 0.3)):
            L = sum(l["length"] for l in allines if lo <= l["frac"] < hi)
            print("   %3.0f-%3.0f%% on-road: %8.0fpx  %5.1f%% of length"
                  % (100 * lo, min(100, 100 * hi), L, 100 * L / total))

        print("\nhow 'X%% of length is bad' moves with the bad-line threshold")
        for thr in (0.3, 0.4, 0.5, 0.6, 0.7, 0.8):
            L = sum(l["length"] for l in allines if l["frac"] < thr)
            n = sum(1 for l in allines if l["frac"] < thr)
            print("   lines below %3.0f%%: %4d lines  %8.0fpx  %5.1f%% of length"
                  % (100 * thr, n, L, 100 * L / total))

        print("\nhow mean agreement moves with the on-road tolerance")
        for tol in (2.0, 3.0, 4.0, 6.0, 8.0, 12.0):
            f = mean_agreement(results, tol)
            print("   tolerance %4.1fpx: mean on-road agreement %5.1f%%"
                  % (tol, 100 * f))
        return results

    if a.offset:
        # If the misaligned lines share a displacement direction, they are one
        # calibration bug.  If they scatter, they are 113 separate geometry
        # problems.  That distinction decides whether this is fixable.
        rows = [l for r in results for l in r["lines"]
                if l["kind"] == "MISALIGNED" and l["frac"] < a.min_line
                and l["conc"] == l["conc"]]
        rows.sort(key=lambda l: -l["length"])
        print("misaligned lines by signed displacement from hand trace:")
        print("   (dx,dy) = pak minus hand, in map px; conc 1.0 = whole line "
              "shifted one way")
        for l in rows[:a.worst or 25]:
            dx, dy = l["off"]
            print("   %+6.1f %+6.1f  |d|=%5.1f  conc=%.2f  %5.0f%%  T%d  "
                  "%-22s len=%5.0fpx"
                  % (dx, dy, np.hypot(dx, dy), l["conc"], 100 * l["frac"],
                     l["tier"], l["_region"], l["length"]))
        good = [l for l in rows if l["conc"] == l["conc"]]
        print("\n   %d misaligned lines, %d close enough for a signed "
              "displacement to mean anything" % (len(rows), len(good)))
        if good:
            dxs = np.array([l["off"][0] for l in good])
            dys = np.array([l["off"][1] for l in good])
            print("   median dx=%+.1f  dy=%+.1f" % (np.median(dxs),
                                                    np.median(dys)))
            print("   median conc=%.2f  -- near 1.0 means each line is shifted "
                  "ONE way (a displacement); near 0 means it scatters"
                  % np.median([l["conc"] for l in good]))
            # A single global calibration error would give every line the SAME
            # vector.  Report the spread, not just the median.
            print("   dx spread: p10=%+.1f p90=%+.1f   dy spread: p10=%+.1f "
                  "p90=%+.1f"
                  % (np.percentile(dxs, 10), np.percentile(dxs, 90),
                     np.percentile(dys, 10), np.percentile(dys, 90)))
        return results

    if a.worst:
        # The long misaligned lines are what decides promotion, so list them by
        # LENGTH rather than by agreement score: a 1200px line at 45% agreement
        # is a far worse defect than a 60px line at 10%, and the score-sorted
        # report buries it among debris.
        cand = [l for r in results for l in r["lines"]
                if l["kind"] == "MISALIGNED" and l["frac"] < a.min_line]
        cand.sort(key=lambda l: -l["length"])
        print("longest misaligned lines (frac < %.2f):" % a.min_line)
        for l in cand[:a.worst]:
            x0, y0, x1, y1 = l["bbox"]
            # render_overlay's --zoom takes OUTPUT pixels (1024 wide), and the
            # loaders emit map pixels (2048 wide), so halve to convert.
            pad = 40
            z = [int(max(0, (x0 - pad) / 2)), int(max(0, (y0 - pad) / 2)),
                 int(min(2048, (x1 + pad) / 2)), int(min(1776, (y1 + pad) / 2))]
            print("  %5.0f%%  T%d  %-22s len=%5.0fpx n=%-4d run %d  "
                  "bbox %4d,%4d..%4d,%4d" % (
                      100 * l["frac"], l["tier"], l["_region"],
                      l["length"], l["npts"], l["run"],
                      x0, y0, x1, y1))
            print("        render_overlay.py %s --zoom %d,%d,%d,%d"
                  % (l["_region"], z[0], z[1], z[2], z[3]))
        tot = sum(l["length"] for l in cand)
        allp = sum(l["length"] for r in results for l in r["lines"])
        print("\n%d misaligned lines, %.0fpx of %.0fpx audited (%.1f%%)"
              % (len(cand), tot, allp, 100 * tot / allp))
        return results

    hdr = "%-22s %5s %5s %6s %6s  %s" % (
        "hex", "hand", "pak", "cover", "onrd", "per-tier %<=4px")
    print(hdr)
    print("-" * len(hdr))
    for res in results:
        per = "  ".join("T%d %3.0f%%" % (k, 100 * v["frac"])
                        for k, v in sorted(res["tiers"].items()))
        print("%-22s %5d %5d %5.0f%% %5.0f%%  %s" % (
            res["region"], res["n_hand"], res["n_pak"],
            100 * res["comparable_frac"], 100 * res["overall"], per))

    # Pooled per-tier view.  Micro-averaged over comparable pixels so a big hex
    # is not outvoted by a small one, matching how the existing netdiff metrics
    # weight.
    print("\npooled per-tier (micro, comparable px only):")
    for k in sorted({k for r in results for k in r["tiers"]}):
        px = sum(r["tiers"][k]["comparable"] for r in results
                 if k in r["tiers"])
        tot = sum(r["tiers"][k]["comparable"] * r["tiers"][k]["frac"]
                  for r in results if k in r["tiers"])
        print("   T%d  %5.1f%% within %gpx   (%d px over %d hexes)"
              % (k, 100 * tot / px if px else 0, ON_ROAD_PX, px,
                 sum(1 for r in results if k in r["tiers"])))

    bad = [(l, r["region"]) for r in results for l in r["lines"]
           if l["frac"] < a.min_line]
    tails = [x for x in bad if x[0]["kind"] == "TAIL"]
    mis = [x for x in bad if x[0]["kind"] == "MISALIGNED"]
    total = sum(len(r["lines"]) for r in results)
    print("\nlines below %.0f%%: %d of %d audited  (%d tail, %d misaligned)"
          % (100 * a.min_line, len(bad), total, len(tails), len(mis)))
    for l, reg in sorted(bad, key=lambda x: x[0]["frac"]):
        b = l["bbox"]
        print("   %5.0f%%  T%d  %-10s %-22s bbox %4d,%4d..%4d,%4d  run %d/%d vtx"
              % (100 * l["frac"], l["tier"], l["kind"], reg,
                 b[0], b[1], b[2], b[3], l["run"], l["npts"]))
    return results


if __name__ == "__main__":
    main()
