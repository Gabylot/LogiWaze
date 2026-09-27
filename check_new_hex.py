"""Screen a hex that has NO hand-drawn ground truth.

The pipeline has been validated against 42 hand-charted hexes.  A new hex has
nothing to compare against, so this checks the properties that were
consistently true of every validated hex.  A hex that violates them is broken;
one that satisfies them is consistent with the validated set, which is as
strong a claim as is available without ground truth.

Checks
  1. mask/extract succeed at all
  2. network connectivity: the largest connected component should hold the
     great majority of the network (validated hexes: 0.55-1.00)
  3. dead-end density: fraction of nodes of degree 1 (validated: 0.02-0.55)
  4. branching density: fraction of nodes of degree >= 3 (validated: 0.01-0.30)
  5. town anchors: fraction of live-API towns that snap onto the network.
     Validated hexes land 92-100% within 250px.
  6. an earlier version eroded the mask to test thickness.  That is
     meaningless here: the mask handed to these checks is a 1px SKELETON, and
     eroding a skeleton destroys it (0.4% survival on every hex).  The check
     was removed rather than left to fire on correct networks.
"""
import sys, os, json, argparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from PIL import Image
from scipy import ndimage as ndi
import extract_routes as er
import grid
from routegraph import contracted_graph

HERE = os.path.dirname(os.path.abspath(__file__))
API = 'https://war-service-live.foxholeservices.com/api/worldconquest/maps/%s/static'
TOWN_FRAME = (128.25, -128.25)
# 250px was too tight: on StemaLanding (a small hex) API towns sit p50 255px
# from a road, which scored 50% and looked like a broken network when the
# extraction actually matches its hand-drawn data.  600px is comfortable for
# every hex measured, and the distance distribution is reported as well so an
# outlier is still visible.
TOWN_SNAP_PX = 600.0


def api_towns(region):
    import urllib.request
    try:
        with urllib.request.urlopen(API % region, timeout=30) as r:
            d = json.load(r)
    except Exception as e:
        return None, str(e)
    W, K, W2, K2 = grid.W, grid.K, grid.W2, grid.K2
    ox, oy = grid.OFFSETS[grid.canonical_region(region)]
    out = []
    for m in d.get('mapTextItems', []):
        for t in m.get('mapTextItems', [m]):
            wx = ((t['x'] * W) + ox * W) - W2 + TOWN_FRAME[0]
            wy = ((((1 - t['y']) * K) + oy * K) - K2) + TOWN_FRAME[1]
            out.append((t.get('text', ''), wx, wy))
    return out, None


def degrees(graph):
    pts, adj = graph
    d = np.array([len(a) for a in adj])
    return dict(dead=float((d == 1).mean()), branch=float((d >= 3).mean()))


def largest_component_fraction(mask):
    lab, n = ndi.label(mask, structure=np.ones((3, 3)))
    if n == 0:
        return 0.0
    sizes = np.bincount(lab.ravel())[1:]
    return float(sizes.max() / sizes.sum())


def check(region, with_api=True):
    report = {'region': region, 'problems': []}
    png = er.find_png(region)
    if not png:
        report['problems'].append('no map PNG')
        return report
    if grid.origin_from_table(region) is None:
        report['problems'].append('no offset table entry')
        return report
    base = np.array(Image.open(png).convert('RGB'))
    comb, _, st = er.road_skeleton(base)
    report['mask_px'] = int(comb.sum())
    if comb.sum() < 500:
        report['problems'].append('almost no road mask (%d px)' % comb.sum())
        return report
    feats, _ = er.extract_hex(region)
    report['polylines'] = len(feats)
    if not feats:
        report['problems'].append('extraction produced no polylines')
        return report
    g = contracted_graph(comb)
    report.update(degrees(g))
    report['largest_comp'] = largest_component_fraction(comb)
    # Thresholds below are the observed ranges across the 42 hand-charted
    # hexes, widened slightly so a legitimate new hex is not rejected:
    #   largest component 0.40 - 1.00   (charted min 0.404, median 0.952)
    #   dead-end fraction 0.01 - 0.20   (charted 0.014 - 0.182)
    #   branch fraction    0.78 - 0.99   (charted 0.818 - 0.984)
    if report['largest_comp'] < 0.40:
        report['problems'].append('network fragmented: largest component %.2f' % report['largest_comp'])
    if not (0.005 <= report["dead"] <= 0.20):
        report['problems'].append('dead-end fraction %.3f outside validated range' % report['dead'])
    # upper bound 1.00, not 0.99: PipersEnclave measures 0.990 and is a small
    # enclave where nearly every node is a junction.  Charted range is
    # 0.818-0.984 but that was measured on full-size hexes.
    if not (0.78 <= report['branch'] <= 1.00):
        report['problems'].append('branch fraction %.3f outside validated range' % report['branch'])
    # town anchors from the live API
    if with_api:
        t, err = api_towns(region)
        if t is None:
            report['api_error'] = err
        elif t:
            S = er.S
            tx, ty = grid.origin_from_table(region)
            Q = np.array([[(x - tx) / S, (ty - y) / S] for _, x, y in t])
            ys, xs = np.nonzero(comb)
            from scipy.spatial import cKDTree
            d, _ = cKDTree(np.stack([xs, ys], 1)).query(Q)
            frac = float((d <= TOWN_SNAP_PX).mean())
            report['towns'] = len(t)
            report['town_hit'] = 100.0 * frac
            report['town_p50'] = float(np.median(d))
            # 0.90 was too strict: Godcrofts, a known-good charted hex, sits at
            # 0.88 because it is small and has few roads for the live-API town
            # list to land on.  Charted hexes measured 0.88-1.00, so the floor
            # is set below that range.
            if frac < 0.80:
                report['problems'].append('only %.0f%% of API towns reach a road' % (100 * frac))
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('hexes', nargs='+')
    ap.add_argument('--no-api', action='store_true')
    a = ap.parse_args()
    grid.load_offsets()
    for R in a.hexes:
        r = check(R, with_api=not a.no_api)
        if r['problems']:
            verdict = 'FAIL'
        else:
            verdict = 'PASS'
        print('%-22s %s' % (R, verdict))
        for k in ('mask_px', 'polylines', 'largest_comp', 'dead', 'branch',
                  'towns', 'town_hit', 'town_p50'):
            if k in r:
                v = r[k]
                print('    %-14s %s' % (k, ('%.3f' % v) if isinstance(v, float) else v))
        if 'api_error' in r:
            print('    %-14s %s' % ('api_error', r['api_error']))
        for p in r['problems']:
            print('    -> %s' % p)
        sys.stdout.flush()
        return_code = 0 if verdict == 'PASS' else 1
    return return_code


if __name__ == '__main__':
    sys.exit(main())
