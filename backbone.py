#!/usr/bin/env python3
"""OCR backbone + pak ONLY where OCR genuinely has no road.

v1 of this filter compared pak VERTICES to OCR VERTICES and kept 1,731 features.
That is wrong: after simplification a road crossing another mid-run has no
near vertices, so 1,599 of those 1,731 sat a median of 0.9 cm from the OCR
backbone - the same road drawn twice.  The map showed doubled roads.

This version compares pak geometry to the OCR geometry as densely sampled
points (segment-to-segment), and keeps a feature only when even its closest
approach is further than --cover.
"""
import argparse, json, math
import numpy as np
from scipy.spatial import cKDTree

SCALE, HALF = 128.0 / 20037500.0, 128.0
STEP = 0.02                       # world units between samples (~1.7 m)


def to_world(c):
    return [(p[0] * SCALE + HALF, p[1] * SCALE - HALF) for p in c]


def densify(pts, step=STEP):
    out = []
    for i in range(len(pts) - 1):
        (x0, y0), (x1, y1) = pts[i], pts[i + 1]
        n = max(1, int(math.hypot(x1 - x0, y1 - y0) / step))
        for k in range(n + 1):
            t = k / n
            out.append((x0 + (x1 - x0) * t, y0 + (y1 - y0) * t))
    return out


def parts_of(f):
    g = f['geometry']
    return [g['coordinates']] if g['type'] == 'LineString' else g['coordinates']


ap = argparse.ArgumentParser()
ap.add_argument('--ocr', required=True)
ap.add_argument('--pak', required=True)
ap.add_argument('--mode', choices=['ocr-first', 'pak-first'], default='pak-first',
                help="pak-first: pak is the network, OCR only fills what pak lacks "
                     "(this is the stated goal).  ocr-first is the earlier, "
                     "inverted arrangement kept for comparison.")
ap.add_argument('--out', required=True)
ap.add_argument('--cover', type=float, default=0.3,
                help='world units; keep pak only where OCR is farther than this (0.3 = 26 m)')
ap.add_argument('--pct', type=float, default=5.0,
                help='closest-approach percentile used as the feature distance')
a = ap.parse_args()

ocr = json.load(open(a.ocr))
pak = json.load(open(a.pak))
if a.mode == 'pak-first':
    primary, filler, pname, fname = pak, ocr, 'pak', 'OCR'
else:
    primary, filler, pname, fname = ocr, pak, 'OCR', 'pak'

pts = []
for f in primary['features']:
    for c in parts_of(f):
        pts.extend(densify(to_world(c)))
tree = cKDTree(np.asarray(pts))
print('  %s backbone: %d features, %d sampled points' % (pname, len(primary['features']), len(pts)))
keep, drop, hist = [], 0, []
for f in filler['features']:
    fp = []
    for c in parts_of(f):
        fp.extend(densify(to_world(c)))
    if not fp:
        drop += 1
        continue
    d = float(np.percentile(tree.query(np.asarray(fp))[0], a.pct))
    hist.append(d)
    if d > a.cover:
        keep.append(f)
    else:
        drop += 1
h = np.asarray(hist)
print('  %s features %d: kept %d as gap-fill, dropped %d (already covered by %s)'
      % (fname, len(filler['features']), len(keep), drop, pname))
if len(h):
    print('  closest approach: p10 %.3f  p50 %.3f  p90 %.3f  max %.3f'
          % (np.percentile(h, 10), np.percentile(h, 50), np.percentile(h, 90), h.max()))
out = {'type': 'FeatureCollection', 'crs': pak.get('crs'),
       'features': primary['features'] + keep}
json.dump(out, open(a.out, 'w'))
print('wrote %s: %d features (%d %s + %d %s gap-fill)  mode=%s'
      % (a.out, len(out['features']), len(primary['features']), pname, len(keep), fname, a.mode))
