"""Accuracy validation: does the extracted network reproduce the hand-drawn one?

The reasoning behind this harness: the pipeline has hand-drawn ground truth for
the hexes that have been charted.  If the extracted road network routes the
same way as the hand-drawn network across all of those, then the same pipeline
applied to an uncharted hex can be trusted.

For every hex with ground truth this reports, per hex:
  towns     how many town anchors were usable
  pairs     how many town-to-town routes were comparable
  p50       median of (extracted distance / hand distance); 1.0 is ideal
  match%    fraction of pairs whose route length is within 5% of the hand route

A null test is included: each hex is also scored against a SHIFTED copy of its
own hand-drawn network.  A hex cannot pass by accident if a broken version of
itself scores no better.
"""
import sys, json, os, argparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from scipy.spatial import cKDTree
import extract_routes as er
import grid
from routegraph import contracted_graph, dijkstra

HERE = os.path.dirname(os.path.abspath(__file__))
ROADS = os.path.join(HERE, 'Roads.json')
TOWNS = os.path.join(HERE, 'towns.json')
# towns.json is in a different world frame than Roads.json; fitted by
# minimising median town-to-road distance (0.5px resolution).
TOWN_FRAME = (128.25, -128.25)
TOWN_SNAP_PX = 250.0
WITHIN = 0.05


def load():
    lz = json.load(open(ROADS))
    byreg = {}
    for f in lz['features']:
        byreg.setdefault(f['properties']['region'], []).append(f)
    towns = json.load(open(TOWNS))
    return byreg, towns


def rasterise(feats, H, W, tx, ty, width=1):
    from PIL import Image, ImageDraw
    im = Image.new('1', (W, H), 0)
    d = ImageDraw.Draw(im)
    S = er.S
    for f in feats:
        c = f['geometry']['coordinates']
        if len(c) > 1:
            d.line([((x - tx) / S, (ty - y) / S) for x, y in c], fill=1, width=width)
    return np.array(im, bool)


def town_pixels(R, towns, tx, ty):
    S = er.S
    out = []
    for v in towns.values():
        if v.get('region') != R:
            continue
        wx, wy = v['x'] + TOWN_FRAME[0], v['y'] + TOWN_FRAME[1]
        out.append((v['name'], (wx - tx) / S, (ty - wy) / S))
    return out


def route_distances(graph, tl, snap):
    """Shortest-path distances between towns, for one graph.

    tl is the (name, x, y) town list.  Each town snaps to its own nearest node
    in THIS graph, so the two networks are never forced onto shared nodes.
    """
    pts, adj = graph
    Q = np.array([[t[1], t[2]] for t in tl])
    d, i = cKDTree(pts).query(Q)
    sn = {t[0]: int(ii) for t, dd, ii in zip(tl, d, i) if dd <= snap}
    names = sorted(sn)
    D = {}
    for a in names:
        da = dijkstra(adj, sn[a])
        for b in names:
            if b <= a:
                continue
            if sn[b] in da:
                D[(a, b)] = da[sn[b]]
    return D


def score(region, byreg, towns, shift=0):
    """shift != 0 scores the hex against a deliberately broken copy of its own
       hand-drawn network, as a null test."""
    from PIL import Image
    base = np.array(Image.open(er.find_png(region)).convert('RGB'))
    H, W = base.shape[:2]
    tx, ty = grid.origin_from_table(region)
    comb, _, _ = er.road_skeleton(base)
    ge = contracted_graph(comb)
    if shift:
        feats = [dict(f) for f in byreg[region]]
        for f in feats:
            f['geometry'] = dict(f['geometry'])
            f['geometry']['coordinates'] = [[c[0] + shift * er.S, c[1] + shift * er.S]
                                            for c in f['geometry']['coordinates']]
    else:
        feats = byreg[region]
    gh = contracted_graph(rasterise(feats, H, W, tx, ty))
    if not ge or not gh:
        return None
    tl = town_pixels(region, towns, tx, ty)
    if len(tl) < 2:
        return None
    PE = route_distances(ge, tl, TOWN_SNAP_PX)
    PH = route_distances(gh, tl, TOWN_SNAP_PX)
    keys = [k for k in PE if k in PH and PH[k] > 20]
    if not keys:
        return None
    rat = np.array([PE[k] / PH[k] for k in keys])
    return dict(region=region, towns=len(set(k for k in PE) | set(k for k in PH)),
                pairs=len(keys), p50=float(np.median(rat)),
                match=100.0 * float((np.abs(rat - 1) < WITHIN).mean()))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('hexes', nargs='*')
    ap.add_argument('--null-test', action='store_true',
                    help='also score each hex against a shifted copy of itself')
    ap.add_argument('--min-pairs', type=int, default=10)
    args = ap.parse_args()
    byreg, towns = load()
    grid.load_offsets()
    regions = args.hexes or sorted(byreg)
    print('%-22s %6s %6s %8s %9s' % ('hex', 'towns', 'pairs', 'p50', 'match%'))
    print('-' * 56)
    good = []
    for R in regions:
        if R not in byreg:
            print('%-22s  no hand-drawn data' % R)
            continue
        try:
            r = score(R, byreg, towns)
        except Exception as e:
            print('%-22s  ERROR %s' % (R, e))
            continue
        if not r:
            print('%-22s  too few comparable routes' % R)
            continue
        print('%-22s %6d %6d %8.3f %8.1f%%' % (R, r['towns'], r['pairs'], r['p50'], r['match']))
        sys.stdout.flush()
        if r['pairs'] >= args.min_pairs:
            good.append(r)
    if not good:
        print('\nno hexes scored with enough pairs')
        return 1
    m = np.array([g['match'] for g in good])
    p = np.array([g['p50'] for g in good])
    print('-' * 56)
    print('%d hexes scored with >=%d pairs' % (len(good), args.min_pairs))
    print('  median p50 route ratio : %.3f  (1.0 = identical routing)' % np.median(p))
    print('  median match%%         : %.1f' % np.median(m))
    print('  hexes p50 within 5%%   : %d/%d' % (int((np.abs(p - 1) < 0.05).sum()), len(p)))
    print('  hexes match%% >= 50    : %d/%d' % (int((m >= 50).sum()), len(m)))
    if args.null_test:
        print()
        print('null test (hand network shifted 60px against itself):')
        n = [score(R, byreg, towns, shift=60) for R in regions if R in byreg]
        n = [x for x in n if x and x['pairs'] >= args.min_pairs]
        for x in n[:8]:
            print('  %-22s p50 %7.3f  match %5.1f%%' % (x['region'], x['p50'], x['match']))
        if n:
            print('  null median p50 %.3f  median match%% %.1f'
                  % (np.median([x['p50'] for x in n]),
                     np.median([x['match'] for x in n])))
    return 0


if __name__ == '__main__':
    sys.exit(main())
