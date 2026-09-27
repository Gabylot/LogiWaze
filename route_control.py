"""CONTROL: compare the HAND network against ITSELF, rasterised at width 1
   versus width 3.  Any ratio inflation here is a measurement artifact of the
   raster width, not extractor error.  Without this control the headline
   numbers cannot be interpreted."""
import sys, json
sys.path.insert(0, '/var/www/LogiWaze')
import numpy as np
from PIL import Image
from scipy.spatial import cKDTree
import extract_routes as er, grid
from routecmp2 import rasterise, TOWN_OFF, SNAP_PX
from routegraph import contracted_graph, dijkstra

lz = json.load(open('/var/www/LogiWaze/Roads.json'))
tw = json.load(open('/var/www/LogiWaze/towns.json'))
byreg = {}
for f in lz['features']:
    byreg.setdefault(f['properties']['region'], []).append(f)
grid.load_offsets()

R = 'ClansheadValleyHex'
base = np.array(Image.open(er.find_png(R)).convert('RGB'))
H, W = base.shape[:2]
tx, ty = grid.origin_from_table(R)
S = er.S
tl = []
for v in tw.values():
    if v.get('region') != R:
        continue
    wx, wy = v['x'] + TOWN_OFF[0], v['y'] + TOWN_OFF[1]
    tl.append({'name': v['name'], 'x': (wx - tx) / S, 'y': (ty - wy) / S})
Q = np.array([[t['x'], t['y']] for t in tl])


def pairs(ph, ah):
    dh, ih = cKDTree(ph).query(Q)
    sh = {t['name']: int(i) for t, d, i in zip(tl, dh, ih) if d <= SNAP_PX}
    names = sorted(sh)
    D = {}
    for a in names:
        da = dijkstra(ah, sh[a])
        for b in names:
            if b <= a:
                continue
            if sh[b] in da:
                D[(a, b)] = da[sh[b]]
    return D


w1, a1 = contracted_graph(rasterise(byreg[R], H, W, tx, ty, 1))
D1 = pairs(w1, a1)
print('hand@width1: %d nodes, %d town pairs' % (len(w1), len(D1)), flush=True)
w3, a3 = contracted_graph(rasterise(byreg[R], H, W, tx, ty, 3))
D3 = pairs(w3, a3)
print('hand@width3: %d nodes, %d town pairs' % (len(w3), len(D3)), flush=True)
keys = [k for k in D1 if k in D3 and D1[k] > 20]
r = np.array([D3[k] / D1[k] for k in keys])
print()
print('CONTROL ratio width3/width1: p50 %.3f p10 %.3f p90 %.3f  (n=%d)'
      % (np.median(r), np.percentile(r, 10), np.percentile(r, 90), len(r)))
print('=> inflation of this size is the measurement floor')
