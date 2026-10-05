#!/usr/bin/env python3
"""Extract the water body from a hex's map image.

Water is far easier than roads: it is a large connected region in a distinct
hue rather than a 1-2 px line.  Measured on the map images:

    FishermansRowHex  #304060  36.3% of pixels   (islands - mostly water)
    EndlessShoreHex   #304060  12.4% of pixels   (coast)

so that colour's share already tracks expected water area.  The rule here is
hue (blue clearly above red) plus darkness, which keeps the pale-blue road
variant (201,202,214) and the magenta decay zone out - both were traps for roads.

Rings are built in PIXEL coordinates and converted to world with both ox and oy
together.  The first version took oy but left ox at its default of 0, so every
ring was emitted at x = 0..25.6 with a correct y; the viewer then shifted them
again and they landed off-screen.  Emitting pixels and converting once, as a
pair, removes the chance of getting half the frame wrong.
"""
import argparse, json
import numpy as np
from PIL import Image
from scipy import ndimage as ndi


def water_mask(im, min_area=2500):
    a = np.asarray(im.convert('RGB')).astype(np.int16)
    R, G, B = a[..., 0], a[..., 1], a[..., 2]
    m = (B > R + 18) & (B > G + 6) & (R < 235)
    m = ndi.binary_opening(m, np.ones((3, 3)))
    lab, n = ndi.label(m)
    if n:
        sizes = np.bincount(lab.ravel())
        keep = sizes >= min_area
        keep[0] = False
        m = keep[lab]
    return m


def mask_rings(mask, step=4, min_pts=24):
    """Ordered boundary rings in PIXEL coordinates (col, row).

    Uses skimage's marching squares, which walks the actual boundary.  The
    previous version sorted boundary pixels by angle around the centroid - for a
    hex with several islands that connects them in angular order rather than
    along the edge, producing a starburst of chords across the shape (visible as
    cyan spokes over GodcroftsHex).
    """
    from skimage import measure
    out = []
    for contour in measure.find_contours(mask.astype(np.uint8), 0.5):
        if len(contour) < min_pts * 3:
            continue
        # contour is (row, col); decimate for size, then emit as (col, row)
        c = contour[::max(1, step)].astype(float)
        pts = [(float(col), float(row)) for row, col in c]
        if len(pts) >= min_pts:
            out.append(pts)
    return out


ap = argparse.ArgumentParser()
ap.add_argument('--hex', action='append', required=True, help='hex name, repeatable')
ap.add_argument('--out', default='water.geojson')
ap.add_argument('--png-dir', default='MapHexes')
args = ap.parse_args()

import grid
grid.load_offsets('scripts/export_major_locations.sh')
S = grid.W / 2048.0                      # world units per pixel

features = []
pxrings = []
for h in args.hex:
    im = Image.open('%s/Map%s.png' % (args.png_dir, h))
    m = water_mask(im)
    frac = 100.0 * float(m.mean())
    ox, oy = grid.hex_origin(h)          # origin of the hex TOP-LEFT
    rings = mask_rings(m)
    # Save the raw mask: for the visualisation it is the ground truth.  Filling
    # contours as polygons loses hole information (an island is a HOLE in the
    # water), so drawing every ring solid marks the islands as water too.
    Image.fromarray((m * 255).astype('uint8')).save('%s/mask_%s.png' % (args.out.rsplit('/', 1)[0] or '.', h))
    for ring in rings:
        world = [(ox + px * S, oy - py * S) for px, py in ring]   # ox AND oy together
        features.append({
            'type': 'Feature',
            'properties': {'region': h, 'kind': 'water',
                           'origin_x': ox, 'origin_y': oy},
            'geometry': {'type': 'Polygon',
                         'coordinates': [[list(pt) for pt in world]]},
        })
        pxrings.append([h, [[list(p) for p in ring]]])
    print('  %-20s water %5.1f%% of image, %d ring(s)' % (h, frac, len(rings)))

json.dump({'type': 'FeatureCollection',
           'crs': {'type': 'name',
                   'properties': {'name': 'urn:ogc:def:crs:EPSG::3857'}},
           'features': features}, open(args.out, 'w'))
json.dump(pxrings, open(args.out + '.px.json', 'w'))
print('wrote %s: %d features (plus .px.json with pixel rings)' % (args.out, len(features)))
