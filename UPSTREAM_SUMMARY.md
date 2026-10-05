# Road extraction from game mesh data

Hi - we've been working on replacing the manual QGIS tracing step. Short
version:

The road network can be read straight out of the game's map assets rather than
traced by hand. `scripts/extract_roads.js` already expects a per-hex JSON with
`T1Road`/`T2Road`/`T3Road` mesh objects, but nothing produced those files. We've
added the producer, using the map data from Tsekho's
[fh_map_exporter](https://github.com/Tsekho/fh_map_exporter).

Compared against the hand-traced file on the 43 hexes they share: 99.2%
geometric agreement within 10 m, similar total road length, and it covers
**53 hexes instead of 43**. The tier comes from the asset name, so it's correct
by construction rather than inferred from pixel colours.

The upside is mainly coverage - the 10 new hexes have no hand-traced data at
all - plus not re-tracing by hand when the map changes.

**One thing worth knowing before regenerating anything:** because roads come
out per hex, each hex's roads stop at its own border a few metres short of the
neighbour's, and routing engines join roads by shared endpoints. The result is
that all cross-hex routing silently fails while intra-hex routing works. There's
a `snap_borders.py` step that fixes it - we hit this one the hard way. Happy to
walk through it.

Nothing in the live app has been replaced; the hand-traced file is untouched.

Open to talking about whether this is worth merging, and if so whether the
traced file should stay as a fallback or be retired.
