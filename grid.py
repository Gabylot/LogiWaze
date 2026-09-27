"""
Analytic image->world origin for a Foxhole hex map PNG.

DEFINITIVE SOURCE: LogiWaze's own scripts/export_major_locations.sh contains a
per-hex offset table expressed in exact multiples of the hex size:

    w  = 256/10                 = 25.6      (hex width;  10 columns span 256)
    k  = w*sqrt(3)/2             = 22.1703   (hex height)
    w2 = w/2                    = 12.8
    k2 = k/2                    = 11.0851

    town_x = (raw_x * w) + offsetx - w2
    town_y = ((1-raw_y) * k) + offsety - k2

DeadLandsHex is the hex with offsetx=0, offsety=0, and the roads confirm it.
Inverting for image pixel (0,0) (the hex's north-west corner, i.e. the town
frame's (-w2, -k2) corner) gives a per-hex origin in world units directly from
the table - no fitting, no reference to neighbouring hexes.

The least-squares model below (derived independently from per-hex fits against
the hand-drawn roads) reproduces the table to within 0.26 world units = 21 px,
and is kept as a cross-check plus a fallback for hexes missing from the table.
"""
import math

WORLD_SPAN = 256.0
HEX_COLS = 10.0

W = WORLD_SPAN / HEX_COLS          # 25.6
K = W * math.sqrt(3) / 2           # 22.1703
W2 = W / 2                         # 12.8
K2 = K / 2                         # 11.0851

# Constant that maps the shell table's frame onto the road world frame.
# The table is expressed around the map centre; Roads.json is offset from it.
#
# RE-FITTED 2026-09-26.  The previous values (115.0985, -116.9410) were set from
# per-hex fits against the hand-drawn Roads.json, so they absorbed a systematic
# misregistration instead of exposing it: scoring hand-drawn road points
# against the extracted road mask gave only 76% within 6px at offset (0,0).
# Sweeping the offset over all 43 hexes that have hand-drawn ground truth shows
# a single broad basin whose optimum is dx=-9.5px, dy=+2.5px (0.68px median
# residual, 86.4% within 3px).  Scale was swept too and is exactly 1.000, so
# this is a pure translation, not a zoom error.  Guard checks: the sign flip
# (+8,-4) collapses to 28%, and hand points from one hex score 0.4% against a
# different hex's map, so the peak is signal rather than mask density.
#
# Both SimpleRoads.json (production) and Roads.json (hand-drawn) peak at the
# same offset, so the two datasets agree with each other and it is this
# mapping - not either dataset - that was wrong.  Effect on measured quality,
# micro-averaged over 43 hexes: F1 55.1 -> 86.5, recall 58% -> 97.3%.
WORLD_ORIGIN_X = 115.21725
WORLD_ORIGIN_Y = -116.90975

# region -> (offsetx in units of W, offsety in units of K), parsed from
# scripts/export_major_locations.sh.  Values are exact quarter/half steps.
OFFSETS = {}


def _key(s):
    """Normalise a region name so spelling variants resolve to one entry.

    The same hex is spelled differently in different places: the map PNG is
    MapStemaLAndingHex.png (stray capital A) and MapMarbanHollowHex.png, while
    the shell table and Roads.json use StemaLandingHex and MarbanHollow.  A
    lookup by literal name therefore misses both, which previously made them
    look absent from the table when they were present.  Compare on
    case-insensitive, punctuation-free keys with any trailing "hex" dropped.
    """
    import re
    return re.sub(r'[^a-z0-9]', '', s.lower()).removesuffix('hex')


def canonical_region(region):
    """Return the spelling used by OFFSETS for `region`, or None."""
    k = _key(region)
    for name in OFFSETS:
        if _key(name) == k:
            return name
    return None


def load_offsets(path=None):
    """Parse the per-hex offset table straight out of the shell script.

    The default path was hardcoded to /var/www/LogiWaze, which only exists on
    the Linux box; on Windows every call raised FileNotFoundError before parsing
    anything. Resolve relative to this file so the script runs in both places.

    Two shapes appear in the file: most entries write the multiplier
    explicitly ("1.5 * $w"), but a handful write a bare "0" with no
    multiplier at all.  Both mean the same thing, so the multiplier is
    optional.  Commented-out lines are skipped - the file carries a stale
    duplicate table lower down.
    """
    import re
    import os
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'scripts', 'export_major_locations.sh')
    txt = open(path).read()
    # Values are written as "0.75 * $w", "-2.25 * $w", or a bare "0"; the
    # negative sign is sometimes written with a leading dot ("-.75").
    num = r'(-?(?:\d+(?:\.\d+)?|\.\d+))'
    for line in txt.splitlines():
        s = line.strip()
        if s.startswith('#'):
            continue
        m = re.search(r'"\$f"\s*=\s*"([A-Za-z]+)"', s)
        if not m or 'offsetx' not in s:
            continue
        region = m.group(1)
        # multiplier optional; a bare number is the value itself
        mx = re.search(r'offsetx=\$\(echo\s*"?' + num + r'(?:\s*\*\s*\$w)?', s)
        my = re.search(r'offsety=\$\(echo\s*"?' + num + r'(?:\s*\*\s*\$k)?', s)
        if mx and my:
            OFFSETS[region] = (float(mx.group(1)), float(my.group(1)))
    return OFFSETS


def origin_from_table(region):
    """Exact world origin of image pixel (0,0), read from the shell table.

    The table's frame is the town frame, whose origin (raw 0,0) sits at
    (ox*W + WORLD_ORIGIN_X, oy*K + WORLD_ORIGIN_Y).  Image pixel (0,0) is the
    north-west corner of the hex, i.e. the town frame's (-W2, -K2) corner, so
    the corner offset cancels and the hex origin is the table offset shifted by
    the constant world origin.

    Region names are resolved through canonical_region() so a hex resolves
    whether it is named by its PNG spelling or its Roads.json spelling.
    """
    name = canonical_region(region)
    if name is None:
        return None
    ox, oy = OFFSETS[name]
    return ox * W + WORLD_ORIGIN_X, oy * K + WORLD_ORIGIN_Y


# Least-squares model fitted independently from per-hex road fits.
_TX_C, _TY_C = 115.21725, -116.90975
_TX_P, _TX_Q = 19.1896, -0.0403
_TY_P, _TY_Q = -11.1100, -22.2314


def origin_from_fit(q, p):
    return (_TX_C + _TX_P * p + _TX_Q * q,
            _TY_C + _TY_P * p + _TY_Q * q)


def hex_origin(region=None, q=None, p=None):
    """Prefer the exact shell-table origin; fall back to the fitted model."""
    if region is not None:
        t = origin_from_table(region)
        if t is not None:
            return t
    if q is None or p is None:
        raise ValueError('need region or (q, p)')
    return origin_from_fit(q, p)


def px_to_world(px, py, tx, ty):
    """Map image pixel (px, py) -> world units.  Scale is W/2048."""
    s = W / 2048.0
    return tx + px * s, ty - py * s
