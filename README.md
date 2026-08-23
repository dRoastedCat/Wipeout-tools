[README.md](https://github.com/user-attachments/files/31343290/README.md)
# WipEout VEX Track Tools (Blender Add-on)

A Blender add-on to import and export the collision geometry and visual track
mesh of **WipEout Pure** track files (`.vex`, VEXX format) directly
inside Blender — no intermediate file formats, no manual offset bookkeeping.

It edits tracks *surgically*: only the byte ranges that store vertex
positions are ever touched. Everything else in the file (node headers,
triangle/index data, textures, materials, unrelated nodes) is copied through
byte-for-byte, unchanged. This means the tool doesn't need to fully
understand — or even preserve correctness of — every part of the VEXX
format; it only needs to locate vertex positions reliably, which has been
validated by round-tripping real edited files back to a byte-identical
result.

## Credits & sources

This tool would not exist without prior reverse-engineering work by others:

- **[PierreBelmondo/vscode-wipeout](https://github.com/PierreBelmondo/vscode-wipeout)**
  (MIT license) — a VS Code extension for WipEout modding. Its TypeScript
  source (`core/formats/vexx/v4/collision.ts`, `mesh.ts`,
  `core/utils/pspgu.ts`, `core/primitive/aabb.ts`, `primitive/mesh.ts`) is
  the ground truth this add-on's binary parsers are ported from. Without
  access to that source, the collision and mesh vertex formats used here
  would only be approximate.
- **[thp.io](https://thp.io) (Thomas Perl)** — original public reverse
  engineering of the outer VEXX node-tree container format (`walk-vex.py`,
  2022), which this add-on's tree walker is based on.
- The PSP homebrew community's documentation of the `sceGu` vertex format
  (`GU_VERTEX_8/16/32BIT`, etc.), which the mesh nodes use internally for
  quantized vertex storage.
- **droastedcat** — maintainer, and the one who did all the real-world
  testing (in Blender and in-game) that found and drove the fixes for every
  bug described in this document.

If you build on this add-on, please keep crediting the sources above —
particularly PierreBelmondo's project, which supplied the actual format
specification.

## What this tool does

WipEout Pure tracks are stored in a binary container format called
**VEXX**. A single `.vex` file is a tree of typed nodes holding everything
about a track environment: geometry, collision data, textures, lights,
sound triggers, and so on.

This add-on understands two specific node types well enough to edit them:

- **Collision nodes** (`collision_floor`, `collision_wall`,
  `collision_reset`, and similar) — store the invisible geometry used for
  physics (what the ship can drive on, crash into, or gets reset by).
  Positions are stored as plain 32-bit floats.
- **Mesh nodes** (VEXX signature `0x11e`) — store the *visible* track and
  scenery geometry, as rendered by the game. Positions are stored
  quantized as 16-bit integers plus a scale factor (per mesh "chunk"), in
  the PSP GPU's native vertex format, arranged as triangle strips.

### Import

`VEX Tools panel → Import VEX Track` reads a `.vex` file and builds Blender
objects, organized under a **`VEX Track`** collection so your Scene
Collection stays tidy:

- `VEX - Collisions Floor`
- `VEX - Collisions Wall`
- `VEX - Collisions Reset`
- `VEX - Collisions Other` (non-standard collision nodes that don't fit the
  usual structure — see "Known limitations" below)
- `VEX - Track Mesh` — used when the file's mesh nodes follow the
  `Track_Shape*` naming convention (one merged object)
- `VEX - Track Surface (guessed)` / `VEX - Landscape (guessed)` — used as a
  **fallback** for files that don't use that naming convention (common on
  full "environment" files that include buildings, props, etc. alongside
  the track). Every mesh node is imported as its own separate object, and
  sorted into one of these two collections by a simple heuristic (does the
  node's name contain "track"?). This is a guess, not a certainty — feel
  free to drag objects between the two collections; it has no effect on
  export correctness either way.

Each imported object carries hidden custom properties (`vex_fmt`,
`vex_offsets`, `vex_scales`, `vex_orig_vcount`) that record exactly where in
the original file each of its vertices came from. These are what make
export possible — see "How export works" below.

### Edit

Move vertices around like any other Blender mesh. That's it — that's the
whole supported editing operation. See **"What NOT to do"** below for why
this is a hard boundary, not just a suggestion.

### Export

`VEX Tools panel → Export VEX Track` re-opens the *original* source file
(the one you imported from — its path is remembered automatically), and for
every object in the scene that carries VEX metadata, writes its current
vertex positions back into a copy of that file, at the exact original byte
offsets. Everything else in the file is left untouched. You choose the
output path, so the original file is never overwritten unless you
explicitly pick the same name.

### How export works internally

- Every vertex, at import time, keeps a record of the exact byte offset (and,
  for mesh nodes, the per-chunk quantization scale) it was read from.
- At export time, the add-on never regenerates or reinterprets the file
  structure. It opens the *original* bytes fresh and overwrites only those
  specific byte ranges with the current (possibly edited) vertex positions,
  converting back from Blender's coordinate space and, for mesh nodes,
  re-quantizing to 16-bit integers using the recorded scale.
- Axis convention: the game uses Y-up, Blender uses Z-up. Import/export
  apply an exact, mutually-inverse conversion (`(x,y,z) → (x,-z,y)` on
  import, its inverse on export) so edits round-trip correctly and the
  track appears upright in Blender's viewport.

This design means the add-on doesn't need to fully understand triangle/index
data, materials, textures, or the dozens of other node types in a `.vex`
file — it only needs to find vertex positions reliably, which has been
verified by re-deriving files with known, real, manually-made edits and
confirming the result is byte-for-byte identical to those edits.

## Installation

1. Download `vex_blender_addon.py`.
2. In Blender: `Edit → Preferences → Add-ons → Install...`, select the file.
3. Enable the checkbox next to "WipEout VEX Track Tools".
4. Open the 3D viewport sidebar (press `N`) — a new **VEX Tools** tab
   appears.

## Usage

1. **Import VEX Track** → pick a `.vex` file.
2. Hide/show collections as needed (👁 icon in the Outliner) to work on one
   category at a time without visual clutter from the others.
3. Select vertices, move them. Nothing else.
4. **Export VEX Track** → pick an output path (ideally *not* the original
   file, so you keep an unmodified backup) → test in-game or in a VEXX
   viewer.

## What NOT to do

The add-on works by matching vertices **1:1, by index order**, against a
list of byte offsets recorded at import time. Anything that changes vertex
*count* or *order* silently breaks that correspondence — the wrong bytes
would get written to the wrong places. To keep exports safe:

- **Never add or remove vertices.** No Extrude, Merge, Delete, Subdivide,
  Loop Cut, Knife, Bridge Edge Loops, Bisect, Boolean modifiers, Decimate,
  Remesh, or anything else that changes topology. Moving is the only
  supported edit.
- **Never merge/join two VEX objects together** (`Ctrl+J`). This mixes
  vertex data from two different byte-offset mappings into one mesh and
  will corrupt the export for both.
- **Be careful with duplicating objects.** Duplicating a VEX object (e.g.
  `Shift+D`) copies its custom properties too, so you can end up with two
  objects that both claim to represent the *same* source vertices. On
  export, whichever one is processed last silently overwrites the other's
  changes at those shared byte offsets — with no error, because nothing
  is technically invalid. If you don't need a duplicate, delete it before
  exporting. If you accidentally imported the same file twice in one
  session, you'll have `.001`-suffixed duplicates of everything; delete the
  unwanted set.
- **Deleting a whole unwanted object is safe.** Unlike editing, removing an
  entire object (e.g. a building you don't care about, in the fallback
  "Landscape" collection) is fine: export simply won't touch that
  geometry, leaving it exactly as it was in the source file.
- **Exit Edit Mode before exporting.** The add-on now does this
  automatically as a safety net, but it's good practice regardless. While
  in Edit Mode, Blender keeps your edits in a working copy (BMesh) that
  isn't reflected on the underlying mesh data until you leave Edit Mode —
  reading vertex positions before that happens returns stale, pre-edit
  values.
- **Keep a backup of the original file.** Export always reads from the
  *original* file path remembered at import time and writes a new file at
  the path you choose — it never modifies the original in place unless you
  deliberately export to the same filename. Still, keep a separate backup;
  a wrong file selected in the export dialog is an easy mistake to make.

## Known limitations

- **Not every node parses.** A handful of node types look superficially
  like standard collision or mesh nodes but aren't (e.g. some
  `collision_wall_*` variants turned out to be transform/instance nodes
  referencing shared geometry elsewhere, not embedded collision data).
  These are detected and skipped safely, with a warning, rather than
  producing garbage data.
- **The track vs. landscape split (fallback mode) is a naming guess.** Files
  without a `Track_Shape*` naming convention have no reliable way — from
  the binary data alone — to distinguish the drivable surface from
  scenery. Double-check the split and move objects between the two
  collections as needed.
- **Triangle strip assembly is a reconstruction, not a byte-exact port** of
  the original renderer's logic for every edge case (e.g. multi-section
  chunks, which haven't been observed in practice so far). This only
  affects how the mesh *looks* in Blender — export never writes topology
  data, only positions, so it cannot corrupt the file even if a specific
  strip's reconstructed shape isn't pixel-perfect.
- **External/shared mesh references aren't resolved.** Some mesh nodes
  don't embed geometry at all — they reference geometry defined elsewhere
  in the file (or in another file entirely). These are detected and
  skipped rather than guessed at.
- **This add-on has only been tested on WipEout Pure (VEXX version 4)
  files.** WipEout Pulse (version 6) uses a slightly different stride
  layout in places; partial support exists in the parsing code but it is
  untested.

## License

The parsing logic in this add-on is a direct port of TypeScript source from
[PierreBelmondo/vscode-wipeout](https://github.com/PierreBelmondo/vscode-wipeout),
used under its MIT license. Consider this add-on MIT-licensed as well
unless a maintainer specifies otherwise.
