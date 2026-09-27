"""
Overlay images: draw the pak roads on top of the real map PNGs.

The numeric metrics (netdiff, chamfer) can all be satisfied by two networks
that are wrong in compensating ways.  An overlay over the actual map is the
check that cannot be fooled: if the roads are in the right place they sit on the
painted roads, and any frame error is immediately visible as a line running off
the road surface.

Three layers, drawn in this order:

  1. the map PNG, desaturated and lightened, so the overlay reads clearly
  2. the HAND-TRACED roads, in magenta (the reference)
  3. the PAK roads, coloured by tier, on top

Where pak and hand agree the two overlay and you see the pak colour.  Where they
diverge, magenta shows through -- which is the whole point.

    python3 render_overlay.py AcrithiaHex DeadLandsHex
    python3 render_overlay.py --all
    python3 render_overlay.py --hand-only

Output: J:/overlays/<Hex>.png

Needs Pillow only -- NOT skimage -- so it runs on a machine with no pixel
toolchain installed.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import grid

HERE = os.path.dirname(os.path.abspath(__file__))
MAPS = os.path.join(HERE, "MapHexes")
OUT = os.environ.get("OVERLAY_DIR", r"J:\overlays")
PAK_JSON = os.environ.get("PAK_JSON_DIR", r"J:\fh_export\_json")
HAND = os.path.join(HERE, "road_source.geojson")

MAP_PX = 2048          # map PNG width; grid.W spans it
SCALE_W = 1024         # output width

# Tier colours stay distinguishable from each other AND under the common
# colour-vision deficiencies: blue/orange/green, not a red/green pairing.
TIER_COLOURS = {
    1: (60, 190, 90),      # gravel/paved - green
    2: (240, 150, 40),     # dirt         - orange
    3: (90, 140, 230),     # mud          - blue
    0: (150, 150, 150),
    4: (150, 150, 150),
}
HAND_COLOUR = (230, 40, 120)   # magenta: never used by a tier


def find_png(region):
    """The map PNG for a hex, tolerating the spelling variants grid.py knows.

    The same hex is spelled MapStemaLAndingHex.png and StemaLandingHex in
    different places, so compare on grid.py's normalised key rather than by
    name -- the same lesson as canonical_region().
    """
    if not os.path.isdir(MAPS):
        return None
    for name in sorted(os.listdir(MAPS)):
        if not name.lower().endswith(".png"):
            continue
        stem = name[3:-4] if name.startswith("Map") else name[:-4]
        if grid._key(stem) == grid._key(region):
            return os.path.join(MAPS, name)
    return None


def load_pak(region):
    """Pak polylines for one hex, in pixels of the map image."""
    path = os.path.join(PAK_JSON, region + ".json")
    if not os.path.isfile(path):
        return None
    import roads_from_pak as rfp
    ox, oy = grid.hex_origin(region)
    s = grid.W / MAP_PX          # world units per map pixel
    return [(tier, [((x - ox) / s, (oy - y) / s) for x, y in line])
            for tier, line in rfp.hex_lines(region)]


def load_hand(region):
    """Hand-traced polylines for one hex, in pixels of the map image."""
    d = json.load(open(HAND, encoding="utf-8"))
    ox, oy = grid.hex_origin(region)
    s = grid.W / MAP_PX
    sc = 128.0 / 20037500.0       # mercator -> world, per scripts/roads.js
    out = []
    for f in d["features"]:
        if f.get("properties", {}).get("region") != region:
            continue
        t = f.get("properties", {}).get("tier")
        g = f["geometry"]
        lines = (g["coordinates"] if g["type"] == "MultiLineString"
                 else [g["coordinates"]])
        for line in lines:
            out.append((t, [(((x * sc + 128) - ox) / s,
                             (oy - (y * sc - 128)) / s) for x, y in line]))
    return out


def render(region, hand_only=False, out_dir=OUT, zoom=None, w=SCALE_W,
           pak_only=False, no_map=False):
    """Render one overlay.  `zoom` is (x0, y0, x1, y1) in OUTPUT pixel
    coordinates; the crop is taken at full map resolution and then scaled to
    `w` so a small area is legible instead of a few pixels wide."""
    from PIL import Image, ImageDraw, ImageEnhance
    png = find_png(region)
    if png is None:
        print("%-22s no map PNG" % region)
        return None
    im = Image.open(png).convert("RGB")

    if zoom:
        # Output coords -> full-res map coords, then crop.
        sx = im.size[0] / float(SCALE_W)
        box = tuple(int(round(v * sx)) for v in zoom)
        box = (max(0, box[0]), max(0, box[1]),
               min(im.size[0], box[2]), min(im.size[1], box[3]))
        if box[2] - box[0] < 8 or box[3] - box[1] < 8:
            raise SystemExit("zoom box too small: %r" % (zoom,))
        im = im.crop(box)
        # Re-derive the mapping from the crop back to the original output
        # frame, so lines land on the right pixels after the crop.
        #
        # Both loaders emit coordinates in MAP pixels of the full image, but
        # (x - ox_px) * k is compared against output pixels, so the crop
        # origin must be converted into the OUTPUT frame first. Dividing the
        # full-res box value by SCALE_W skips that conversion and is wrong
        # unless SCALE_W == MAP_PX; it displaced the whole overlay by a
        # factor of 2 and drew it off-canvas.
        k = w / float(im.size[0])
        ox_px = box[0] * SCALE_W / float(MAP_PX)
        oy_px = box[1] * SCALE_W / float(MAP_PX)
        im = im.resize((max(1, int(im.size[0] * k)),
                        max(1, int(im.size[1] * k))), Image.LANCZOS)
    else:
        k = SCALE_W / float(im.size[0])     # downscale factor
        ox_px = oy_px = 0.0
        im = im.resize((max(1, int(im.size[0] * k)),
                        max(1, int(im.size[1] * k))), Image.LANCZOS)
    w, h = im.size

    # Desaturate and lighten so the overlay colours dominate the map. With
    # --no-map the image is flattened to white instead, so a line is only
    # visible where one of the two sources actually put it -- this removes
    # the desaturated terrain (whose out-of-bounds hatching is a similar
    # pinkish tone) as a source of false positives.
    if no_map:
        im = Image.new("RGB", im.size, (255, 255, 255))
    else:
        im = ImageEnhance.Color(im).enhance(0.25)
        im = ImageEnhance.Brightness(im).enhance(1.45)
        im = ImageEnhance.Contrast(im).enhance(0.85)

    d = ImageDraw.Draw(im, "RGBA")
    lw = max(2, int(round(2.2 * k * (w / 1024.0))))

    def draw(polys, colour, width, alpha):
        for _t, pts in polys:
            if len(pts) < 2:
                continue
            d.line([((x - ox_px) * k, (y - oy_px) * k) for x, y in pts],
                   fill=colour + (alpha,), width=width, joint="curve")

    hand = load_hand(region)
    pak = load_pak(region) or []
    by_tier = {}
    for t, pts in pak:
        by_tier.setdefault(t, []).append((t, pts))

    # Each source is drawn only when it is not the one being suppressed, so
    # --hand-only shows magenta alone and --pak-only shows tiers alone.
    # Count both sources regardless of suppression, so the printout always
    # reflects the underlying data rather than what this pass drew.
    n_hand, n_pak = len(hand), len(pak)

    # Suppress only the other source. These are two independent conditions:
    # combining them (as in `not hand_only and not pak_only`) wrongly hides
    # the hand layer in --hand-only mode and renders an empty image.
    if pak_only:
        hand = []
    if hand_only:
        by_tier = {}
    draw(hand, HAND_COLOUR, lw + 2, 150)
    for t in sorted(by_tier):
        draw(by_tier[t], TIER_COLOURS.get(t, (200, 200, 200)), lw, 235)

    # Legend, so the colours are not a guessing game. Only the layers that
    # were actually drawn are listed.
    entries = [("pak gravel/paved", TIER_COLOURS[1]),
               ("pak dirt", TIER_COLOURS[2]),
               ("pak mud", TIER_COLOURS[3])]
    if not pak_only:
        entries.append(("hand-traced", HAND_COLOUR))
    box_h = 14 + 16 * len(entries) + 10
    d.rectangle([8, 8, 190, 8 + box_h], fill=(0, 0, 0, 175))
    yy = 15
    for label, col in entries:
        d.line([(16, yy + 4), (36, yy + 4)], fill=col + (255,), width=lw + 1)
        d.text((44, yy), label, fill=(255, 255, 255, 255))
        yy += 16
    d.text((8, 18 + 16 * len(entries)), "%s  %d x %d" % (region, w, h),
           fill=(255, 255, 255, 255))

    os.makedirs(out_dir, exist_ok=True)
    name = region
    if pak_only:
        name += "_pakonly"
    elif hand_only:
        name += "_handonly"
    if zoom:
        name += "_zoom%d_%d_%d_%d" % tuple(zoom)
    if no_map:
        name += "_nomap"
    out = os.path.join(out_dir, name + ".png")
    im.save(out)
    print("%-22s %s   hand %d, pak %d" % (region, out, n_hand, n_pak))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("hexes", nargs="*")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--hand-only", action="store_true",
                    help="draw only the hand trace (magenta)")
    ap.add_argument("--pak-only", action="store_true",
                    help="draw only the pak roads, by tier")
    ap.add_argument("--no-map", action="store_true",
                    help="flatten the background to white, leaving only overlay")
    ap.add_argument("--zoom", help="x0,y0,x1,y1 in output pixels, e.g. 0,600,400,900")
    ap.add_argument("--width", type=int, default=SCALE_W,
                    help="output width in px for a zoomed crop")
    a = ap.parse_args()
    zoom = None
    if a.zoom:
        try:
            zoom = tuple(int(v) for v in a.zoom.split(","))
        except ValueError:
            raise SystemExit("--zoom wants four ints: x0,y0,x1,y1")
        if len(zoom) != 4 or zoom[2] <= zoom[0] or zoom[3] <= zoom[1]:
            raise SystemExit("--zoom needs x0<x1 and y0<y1")
    grid.load_offsets(os.path.join(HERE, "scripts", "export_major_locations.sh"))
    if a.all:
        hexes = sorted(r for r in grid.OFFSETS
                       if os.path.isfile(os.path.join(PAK_JSON, r + ".json")))
    else:
        hexes = a.hexes
    if not hexes:
        raise SystemExit("give some hex names, or --all")
    if a.hand_only and a.pak_only:
        raise SystemExit("--hand-only and --pak-only are mutually exclusive")
    for hx in hexes:
        render(hx, hand_only=a.hand_only, zoom=zoom, w=a.width,
               pak_only=a.pak_only, no_map=a.no_map)


if __name__ == "__main__":
    main()
