"""Routing-equivalence metric, v2.

   For each hex:
     - build a contracted routable graph from the EXTRACTED skeleton
     - build the same from the HAND-DRAWN roads, rasterised to pixels
     - snap each town to its nearest node in both
     - compare shortest-path distances town-to-town

   Reported as the distribution of extracted/hand ratio.  Lateral error does
   not move this metric at all, which is exactly what we want: a road drawn
   4px off-centre still gives the same route.
"""
import sys, json, time
sys.path.insert(0, '/var/www/LogiWaze')
import numpy as np
from PIL import Image, ImageDraw
from scipy.spatial import cKDTree
import extract_routes as er, grid
from routegraph import contracted_graph, dijkstra

TOWN_OFF = (128.25, -128.25)
# Towns do NOT sit on their roads: measured distance from town to nearest
# hand-drawn road ranges from 3.6px to 192px, because the hand-drawn network
# does not reach every town.  So snap each town to its nearest node in each
# graph independently, with a generous cap, and drop only towns that are
# genuinely isolated in one of the two networks.
SNAP_PX = 250.0


def rasterise(feats, H, W, tx, ty, width=1):
    # width MUST be 1.  A control run comparing the hand network against
    # itself at width 1 vs 3 gave ratio p50 1.058 (p10-p90 0.919-1.267) -
    # the same spread the extractor shows - so a wider raster is a large
    # measurement artifact, not a real difference.
    """Roads.json is in WORLD units; the graph works in pixels."""
    S = er.S
    im = Image.new('1', (W, H), 0)
    d = ImageDraw.Draw(im)
    for f in feats:
        c = f['geometry']['coordinates']
        if len(c) > 1:
            px = [((x - tx) / S, (ty - y) / S) for x, y in c]
            d.line(px, fill=1, width=width)
    return np.array(im, bool)


def hex_analysis(R, byreg, towns):
    base = np.array(Image.open(er.find_png(R)).convert('RGB'))
    H, W = base.shape[:2]
    tx, ty = grid.origin_from_table(R)
    S = er.S
    comb, tm, st = er.road_skeleton(base)
    ge = contracted_graph(comb)
    gh = contracted_graph(rasterise(byreg[R], H, W, tx, ty))
    if not ge or not gh:
        return None
    (pe, ae), (ph, ah) = ge, gh
    # towns live in world units; the graphs are in PIXELS, so convert here or
    # every town lands ~350px from the network
    tl = []
    for v in towns.values():
        if v.get('region') != R:
            continue
        wx, wy = v['x'] + TOWN_OFF[0], v['y'] + TOWN_OFF[1]
        tl.append({'name': v['name'],
                   'x': (wx - tx) / S,
                   'y': (ty - wy) / S})
    if len(tl) < 2:
        return None
    te, th = cKDTree(pe), cKDTree(ph)
    Q = np.array([[t['x'], t['y']] for t in tl])
    de, ie = te.query(Q)
    dh, ih = th.query(Q)
    se = {t['name']: int(i) for t, d, i in zip(tl, de, ie) if d <= SNAP_PX}
    sh = {t['name']: int(i) for t, d, i in zip(tl, dh, ih) if d <= SNAP_PX}
    common = sorted(set(se) & set(sh))
    if len(common) < 2:
        return dict(R=R, ntown=len(tl), nsnap=len(common), note='too few towns')
    PE, PH = {}, {}
    # dijkstra returns distances keyed by NODE INDEX, so the membership test
    # must be on the node index of b, not on b's town name
    for a in common:
        da = dijkstra(ae, se[a])
        for b in common:
            if b <= a:
                continue
            if se[b] in da:
                PE[(a, b)] = da[se[b]]
    for a in common:
        da = dijkstra(ah, sh[a])
        for b in common:
            if b <= a:
                continue
            if sh[b] in da:
                PH[(a, b)] = da[sh[b]]
    keys = [k for k in PE if k in PH and PH[k] > 20]
    if not keys:
        return dict(R=R, ntown=len(tl), nsnap=len(common), note='no common pairs')
    rat = np.array([PE[k] / PH[k] for k in keys])
    dev = np.array([abs(PE[k] - PH[k]) / PH[k] for k in keys])
    return dict(R=R, ntown=len(tl), nsnap=len(common), npairs=len(keys),
                ne=len(pe), nh=len(ph),
                p50=np.median(rat), p10=np.percentile(rat, 10), p90=np.percentile(rat, 90),
                dev50=np.median(dev), dev90=np.percentile(dev, 90),
                within5=100.0 * (dev < 0.05).mean(), within10=100.0 * (dev < 0.10).mean())


if __name__ == '__main__':
    lz = json.load(open('/var/www/LogiWaze/Roads.json'))
    towns = json.load(open('/var/www/LogiWaze/towns.json'))
    byreg = {}
    for f in lz['features']:
        byreg.setdefault(f['properties']['region'], []).append(f)
    grid.load_offsets()
    print('%-22s %5s %5s %6s  %-22s %s' % ('hex', 'towns', 'pairs', 'graph', 'ratio p50/p10/p90', 'within 5% / 10%'))
    for R in sys.argv[1:]:
        t0 = time.time()
        if R not in byreg:
            print('%-22s  no hand-drawn data' % R); continue
        r = hex_analysis(R, byreg, towns)
        if r is None or 'note' in r:
            print('%-22s  %s' % (R, (r or {}).get('note', 'empty'))); continue
        print('%-22s %5d %5d %6d  %5.3f/%5.3f/%5.3f   %5.1f%% / %5.1f%%  (%.0fs)' % (
            R, r['nsnap'], r['npairs'], r['ne'],
            r['p50'], r['p10'], r['p90'], r['within5'], r['within10'],
            time.time() - t0), flush=True)
