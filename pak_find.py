"""
Search the pak for a mesh name and report what the file says around it.

WHY
---
"Is TownSidewalk actually a road, or decoration beside one?" was asked twice and
argued from the name, which proves nothing.  The pak is the only authority: it
holds the mesh's material and texture references, and a footpath/sidewalk mesh
would name a footpath texture while a road mesh would name a road material.

This reads the pak directly and prints the ASCII strings in a window around
each hit, which is enough to see the material/texture path a mesh is bound to
without unpacking Unreal assets.  It is a search tool, not a parser.

    python3 pak_find.py TownSidewalk03
    python3 pak_find.py --context 4000 RoadT1Dirt01
"""
import argparse
import os
import re
import sys

DEFAULT_PAK = (r"E:\Program Files (x86)\Steam\steamapps\common\Foxhole"
               r"\War\Content\Paks\War-WindowsNoEditor.pak")
CHUNK = 1 << 22          # 4 MiB read window
# The pak path contains spaces, and passing it through PowerShell's
# Start-Process -ArgumentList splits it into separate arguments.  Writing the
# path to a one-line file and passing THAT avoids the quoting problem entirely.
PAK_PATH_FILE = "pak_path.txt"


def resolve_pak(arg):
    if arg:
        return arg
    if os.path.isfile(PAK_PATH_FILE):
        p = open(PAK_PATH_FILE, encoding="utf-8").read().strip()
        if p:
            return p
    return DEFAULT_PAK



def scan(path, needle, max_hits=8, context=1200):
    """Print the printable-string neighbourhood of every occurrence."""
    pat = re.compile(re.escape(needle).encode("ascii"), re.IGNORECASE)
    hits = 0
    with open(path, "rb") as f:
        pos = 0
        carry = b""
        while True:
            buf = f.read(CHUNK)
            if not buf:
                break
            data = carry + buf
            base = pos - len(carry)
            for m in pat.finditer(data):
                s = max(0, m.start() - context)
                e = min(len(data), m.end() + context)
                window = data[s:e]
                # keep printable runs of 6+ chars
                for sm in re.finditer(rb"[ -~]{6,}", window):
                    txt = sm.group().decode("ascii", "replace")
                    print("  @%10d  %s" % (base + s + sm.start(), txt))
                hits += 1
                if hits >= max_hits:
                    print("  (stopping at %d hits)" % hits)
                    return hits
            carry = data[-64:]
            pos += len(buf)
    return hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("needle")
    ap.add_argument("--pak", help="pak path; defaults to ./pak_path.txt then "
                                  "the Steam install path")
    ap.add_argument("--max-hits", type=int, default=8)
    ap.add_argument("--context", type=int, default=1200)
    a = ap.parse_args()
    pak = resolve_pak(a.pak)
    if not os.path.isfile(pak):
        raise SystemExit("no pak at %s\n  write the path into %s to avoid "
                         "shell quoting problems"
                         % (pak, PAK_PATH_FILE))
    print("searching %s for %r" % (os.path.basename(pak), a.needle))
    n = scan(pak, a.needle, a.max_hits, a.context)
    print("  %d hits" % n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
