"""Direct network-difference metric.

Why the previous metrics were weak
----------------------------------
Route-LENGTH comparison asks "how far is it from A to B?".  Two networks can
have almost identical total length and share no roads at all, so the metric is
nearly blind to WHERE the networks differ.  The point-distance precision/recall
metric had the opposite problem: a fixed tolerance made it swing 30 points on a
sub-pixel change.

What this measures instead
--------------------------
Treat each network as a set of pixels and measure the bidirectional distance
between them (a chamfer / Hausdorff-style shape distance):

  forward   for every pixel of the EXTRACTED network, the distance to the
            nearest pixel of the HAND-DRAWN network
            -> large values mean "we drew roads that are not there"
  backward  for every pixel of the HAND-DRAWN network, the distance to the
            nearest pixel of the EXTRACTED network
            -> large values mean "we missed roads that are there"

Keeping the two directions separate is the point.  A single symmetric number
hides the difference between inventing a motorway and missing a village road.

The full forward/backward distributions are reported, not just a mean, so the
shape of the disagreement is visible.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from scipy import ndimage as ndi
from PIL import Image, ImageDraw
import extract_routes as er
import grid


def rasterise(feats, H, W, tx, ty, width=1):
    """Hand-drawn roads are in world units; this returns a pixel mask."""
    S = er.S
    im = Image.new('1', (W, H), 0)
    d = ImageDraw.Draw(im)
    for f in feats:
        c = f['geometry']['coordinates']
        if len(c) > 1:
            d.line([((x - tx) / S, (ty - y) / S) for x, y in c], fill=1, width=width)
    return np.array(im, bool)


def chamfer(a, b):
    """Bidirectional pixel distance between two boolean masks.

    Returns (fwd, bwd): distances in px from each pixel of a to the nearest
    pixel of b, and vice versa.
    """
    if not a.any() or not b.any():
        return None, None
    dt_b = ndi.distance_transform_edt(~b)
    dt_a = ndi.distance_transform_edt(~a)
    fwd = dt_b[a]
    bwd = dt_a[b]
    return fwd, bwd


def structural(a, b):
    """Cheap shape descriptors that catch things a distance metric misses."""
    def desc(m):
        lab, n = ndi.label(m, structure=np.ones((3, 3)))
        skel_len = int(m.sum())
        return dict(px=skel_len, comps=n,
                    longest=int(np.bincount(lab.ravel())[1:].max()) if n else 0)
    return desc(a), desc(b)


def compare(extracted_mask, hand_mask, tols=(2, 4, 6, 10)):
    fwd, bwd = chamfer(extracted_mask, hand_mask)
    if fwd is None:
        return None
    out = {}
    for nm, d in (('fwd', fwd), ('bwd', bwd)):
        out[nm + '_p50'] = float(np.percentile(d, 50))
        out[nm + '_p90'] = float(np.percentile(d, 90))
        out[nm + '_p99'] = float(np.percentile(d, 99))
        out[nm + '_mean'] = float(d.mean())
        for t in tols:
            out['%s_within%d' % (nm, t)] = 100.0 * float((d <= t).mean())
    da, db = structural(extracted_mask, hand_mask)
    out['px_ratio'] = da['px'] / max(1, db['px'])
    out['comps'] = (da['comps'], db['comps'])
    return out


def region_masks(region, hand_feats, extracted_mask=None, width=1):
    base = np.array(Image.open(er.find_png(region)).convert('RGB'))
    H, W = base.shape[:2]
    tx, ty = grid.origin_from_table(region)
    if extracted_mask is None:
        extracted_mask, _, _ = er.road_skeleton(base)
    hand = rasterise(hand_feats, H, W, tx, ty, width)
    return extracted_mask, hand


if __name__ == '__main__':
    import json
    from argparse import ArgumentParser
    ap = ArgumentParser()
    ap.add_argument('hexes', nargs='*')
    a = ap.parse_args()
    lz = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'Roads.json')))
    byreg = {}
    for f in lz['features']:
        byreg.setdefault(f['properties']['region'], []).append(f)
    grid.load_offsets()
    regs = a.hexes or sorted(byreg)
    print('%-22s %6s %6s %7s %7s %7s %7s %7s' %
          ('hex', 'fwd50', 'bwd50', 'fwd<=4', 'bwd<=4', 'fwd<=10', 'bwd<=10', 'pxratio'))
    print('-' * 74)
    for R in regs:
        if R not in byreg:
            print('%-22s  no hand-drawn data' % R)
            continue
        em, hm = region_masks(R, byreg[R])
        r = compare(em, hm)
        if not r:
            print('%-22s  empty' % R)
            continue
        print('%-22s %6.2f %6.2f %6.1f%% %6.1f%% %6.1f%% %6.1f%% %7.2f' %
              (R, r['fwd_p50'], r['bwd_p50'], r['fwd_within4'], r['bwd_within4'],
               r['fwd_within10'], r['bwd_within10'], r['px_ratio']))
        sys.stdout.flush()
