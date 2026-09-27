"""
Read a StaticMesh's real bounding box out of the pak.

WHY THIS EXISTS
---------------
`roads_from_blueprints.py` reconstructs town-street geometry from placed
instances.  A placement record gives position, yaw and scale but NOT the mesh's
size, so the piece length has to come from somewhere.  Inferring it from
neighbour spacing was tried and does not work: pieces of one mesh interleave
with other meshes in the town grid, so consecutive gaps within a mesh are
dominated by unrelated pieces (observed for TownSidewalk04_C: 1000 cm x19
alongside 2 cm, 14 cm, 394 cm).  That noise fragmented the network.

The mesh's real extent IS in the pak, as the StaticMesh's BoxSphereBounds.  The
catch is that the bounds are stored as binary floats in the export data, not as
readable text -- `BoxSphereBounds` only appears as a name-table entry, which is
why grepping for it finds the field name and nothing useful.

This walks the pak's index, finds each StaticMesh export, and reads the float
that follows the BoxSphereBounds field.  It is deliberately narrow: it reports a
box per mesh and nothing else, and it prints how confident it is, because a
wrong length silently breaks every downstream join.

    python3 pak_bounds.py TownSidewalk03 RoadT1Dirt01
"""
import argparse
import os
import re
import struct
import sys

PAK_PATH_FILE = "pak_path.txt"
DEFAULT_PAK = (r"E:\Program Files (x86)\Steam\steamapps\common\Foxhole"
               r"\War\Content\Paks\War-WindowsNoEditor.pak")
CHUNK = 1 << 22


def resolve_pak(arg):
    if arg:
        return arg
    if os.path.isfile(PAK_PATH_FILE):
        p = open(PAK_PATH_FILE, encoding="utf-8").read().strip()
        if p:
            return p
    return DEFAULT_PAK


def find_offsets(path, names):
    """Byte offsets of the first occurrence of each name in the pak."""
    pats = {n: re.compile(re.escape(n).encode("ascii")) for n in names}
    out = {n: [] for n in names}
    with open(path, "rb") as f:
        pos = 0
        carry = b""
        while True:
            buf = f.read(CHUNK)
            if not buf:
                break
            data = carry + buf
            base = pos - len(carry)
            for n, p in pats.items():
                for m in p.finditer(data):
                    out[n].append(base + m.start())
            carry = data[-256:]
            pos += len(buf)
    return out


def floats_near(path, offset, span=4096, count=96):
    """Candidate float32 values in a window, with the plausible ones kept.

    Unreal's export is a serialised property stream, so the bounds sit as
    FProperty tagged floats shortly after the name.  Rather than assume a fixed
    layout, this returns every plausible float in the window and lets the
    caller pick, since the mesh origin is usually 0 and the extent is the
    number of interest.
    """
    with open(path, "rb") as f:
        f.seek(max(0, offset - span))
        blob = f.read(span * 2)
    out = []
    for i in range(0, len(blob) - 4):
        v = struct.unpack_from("<f", blob, i)[0]
        if v == 0.0:
            continue
        if 1.0 <= abs(v) <= 1e6 and abs(v) == abs(v):
            out.append((offset - span + i, v))
    return out[:count]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("meshes", nargs="+")
    ap.add_argument("--pak")
    a = ap.parse_args()
    pak = resolve_pak(a.pak)
    if not os.path.isfile(pak):
        raise SystemExit("no pak at %s" % pak)
    offs = find_offsets(pak, a.meshes)
    for m in a.meshes:
        o = offs[m]
        print("== %s : %d occurrence(s)" % (m, len(o)))
        if not o:
            continue
        vals = floats_near(pak, o[0])
        # The extent is the largest plausible value that is not an absurd
        # coordinate; print the top few so the reader can judge.
        vals_sorted = sorted(vals, key=lambda t: -abs(t[1]))[:8]
        for off, v in sorted(vals_sorted):
            print("   @%12d  %12.3f" % (off, v))
    return 0


if __name__ == "__main__":
    sys.exit(main())
