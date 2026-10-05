#!/usr/bin/env python3
"""Drop OCR features that just re-trace a feature already kept.

12.4% of the OCR backbone is duplicated: a sampled point has a twin belonging to
a different feature within 2 cm.  The gap between 2 cm (11.0%) and 30 cm (12.4%)
is empty, so these are not parallel carriageways - they are the same line traced
twice, the signature of the colour mask producing two skeletons for one painted
road.

A real pair of roads crossing at a junction shares only that junction, so it
touches for a few percent of its length.  A duplicate overlaps for nearly all of
it.  So: mark a feature as a re-trace when most of its samples sit on top of a
sample owned by a different feature, union those together, and keep the longest
feature in each group.
"""
import argparse, json, math
import numpy as np
from scipy.spatial import cKDTree

SCALE, HALF = 128.0 / 20037500.0, 128.0
STEP = 0.02                  # world units between samples (~1.7 m)
MU = 2200.0 / 25.6
K = 8                        # neighbours per query


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


def sample(f):
    g = f['geometry']
    parts = [g['coordinates']] if g['type'] == 'LineString' else g['coordinates']
    out = []
    for c in parts:
        out.extend(densify(to_world(c)))
    return out


def length_km(fs):
    n = 0
    for f in fs:
        g = f['geometry']
        for c in ([g['coordinates']] if g['type'] == 'LineString' else g['coordinates']):
            for i in range(len(c) - 1):
                n += math.hypot(c[i+1][0] - c[i][0], c[i+1][1] - c[i][1]) * MU
    return n / 1000.0


ap = argparse.ArgumentParser()
ap.add_argument('--in', dest='src', required=True)
ap.add_argument('--out', dest='dst', required=True)
ap.add_argument('--tol', type=float, default=0.02, help='world units (0.02 = 1.7 m)')
ap.add_argument('--frac', type=float, default=0.6,
                help='a feature is a re-trace if this fraction of it lies on another feature')
a = ap.parse_args()

d = json.load(open(a.src))
feats = d['features']
nfeat = len(feats)

all_pts, owner = [], []
for i, f in enumerate(feats):
    for p in sample(f):
        all_pts.append(p)
        owner.append(i)
A = np.asarray(all_pts, float)
own = np.asarray(owner)
print('  %d features, %d sampled points' % (nfeat, len(A)))

tree = cKDTree(A)
dist, idx = tree.query(A, k=K)
if idx.ndim == 1:
    dist = dist.reshape(K, -1).T
    idx = idx.reshape(K, -1).T

best_d = np.full(len(A), np.inf)
best_o = np.full(len(A), -1, dtype=np.int64)
for c in range(idx.shape[1]):
    o = own[idx[:, c]]
    take = (o != own) & (dist[:, c] < best_d)
    best_d[take] = dist[take, c]
    best_o[take] = o[take]

counts = np.bincount(own, minlength=nfeat).astype(float)
dup_frac = np.zeros(nfeat)
for i in range(nfeat):
    m = own == i
    if m.any():
        dup_frac[i] = float((best_d[m] < a.tol).sum()) / float(m.sum())

parent = list(range(nfeat))


def find(x):
    while parent[x] != x:
        parent[x] = parent[parent[x]]
        x = parent[x]
    return x


def union(x, y):
    rx, ry = find(x), find(y)
    if rx != ry:
        parent[ry] = rx


for i in range(nfeat):
    if counts[i] == 0 or dup_frac[i] < a.frac:
        continue
    m = own == i
    partners = np.unique(best_o[m][best_d[m] < a.tol])
    for j in partners:
        j = int(j)
        if j >= 0 and counts[j] > 0 and dup_frac[j] >= a.frac:
            union(i, j)

groups = {}
for i in range(nfeat):
    groups.setdefault(find(i), []).append(i)

kept_idx = []
for members in groups.values():
    live = [m for m in members if counts[m] > 0]
    kept_idx.append(max(live or members, key=lambda m: len(sample(feats[m]))))

keep = set(kept_idx)
out = [feats[i] for i in range(nfeat) if i in keep]
d['features'] = out
json.dump(d, open(a.dst, 'w'))

dup = [i for i in range(nfeat) if counts[i] > 0 and dup_frac[i] >= a.frac]
print('dedupe_features: %d -> %d features' % (nfeat, len(out)))
print('  features >=%.0f%% coincident with another: %d' % (a.frac * 100, len(dup)))
live = dup_frac[counts > 0]
print('  coincident fraction: p50 %.2f  p90 %.2f  p99 %.2f'
      % (np.percentile(live, 50), np.percentile(live, 90), np.percentile(live, 99)))
print('  length %.0f km -> %.0f km  (tol %.1f m, frac %.2f)'
      % (length_km(feats), length_km(out), a.tol * MU, a.frac))
