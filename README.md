# WipEout VEX Track Tools — Blender Add-on

A Blender add-on to import, visually edit, and export the **collision geometry**,
**visible track mesh**, and **racing line** of **WipEout Pure / Pulse** track
files (`.vex`, VEXX format) directly inside Blender — no intermediate file
formats, no manual offset bookkeeping.

This repository contains **only the Blender add-on**. It includes:

1. **Geometry import/export** — collision, track mesh, pads, and textures.
2. **Racing-line tools** — editable lane lines, an AI path Forward/Reverse toggle,
   and fine-tuning of the AI racing line.
3. The **full documentation of the WipEout racing/AI system**, including the
   reverse-engineering of the `WO_TRACK` racing-line node and the exact process
   used to invert the AI, with its known limitations.

---

## Table of contents

- [1. Overview](#1-overview)
- [2. Installation](#2-installation)
- [3. What the add-on understands](#3-what-the-add-on-understands)
- [4. Import](#4-import)
- [5. Edit](#5-edit)
- [6. Export](#6-export)
- [7. Racing-line tools (lane lines & AI path)](#7-racing-line-tools-lane-lines--ai-path)
- [8. What NOT to do](#8-what-not-to-do)
- [9. Known limitations](#9-known-limitations)
- [10. Credits](#10-credits)
- [Appendix A — The AI / racing system](#appendix-a--the-ai--racing-system)
  - [A.1 Track file structure (`.vex`)](#a1-track-file-structure-vex)
  - [A.2 The `wotrack_` node](#a2-the-wotrack_-node)
  - [A.3 How the AI uses the `wotrack_`](#a3-how-the-ai-uses-the-wotrack_)
  - [A.4 How to successfully reverse the AI](#a4-how-to-successfully-reverse-the-ai)
  - [A.5 The reverse algorithm](#a5-the-reverse-algorithm)
  - [A.6 Troubleshooting](#a6-troubleshooting)
  - [A.7 The Quake deformation (known limitation)](#a7-the-quake-deformation-known-limitation)
- [References](#references)

---

## 1. Overview

A Blender add-on to import and export the collision geometry and visual track
mesh of **WipEout Pure / Pulse** track files (`.vex`, VEXX format) directly
inside Blender — no intermediate file formats, no manual offset bookkeeping.

It edits tracks *surgically*: only the byte ranges that store vertex positions
are ever touched. Everything else in the file (node headers, triangle/index data,
textures, materials, unrelated nodes) is copied through byte-for-byte, unchanged.
This means the tool doesn't need to fully understand — or even preserve
correctness of — every part of the VEXX format; it only needs to locate vertex
positions reliably, which has been validated by round-tripping real edited files
back to a byte-identical result.

In addition to import/export, the add-on exposes **racing-line tools** that make
it possible to visualize the AI path, fix spots where the AI hugs the wall, and
toggle the racing direction (Forward/Reverse) directly from the Blender UI.

## 2. Installation

1. Download `vex_blender_addon.py`.
2. In Blender: `Edit → Preferences → Add-ons → Install...`, select the file.
3. Enable the checkbox next to **"WipEout VEX Track Tools"**.
4. Open the 3D viewport sidebar (press `N`) — a new **VEX Tools** tab appears.

### Usage quick start

1. **Import VEX Track** → pick a `.vex` file.
2. Hide/show collections as needed (👁 icon in the Outliner) to work on one
   category at a time without visual clutter from the others.
3. Select vertices, move them. Nothing else.
4. **Export VEX Track** → pick an output path (ideally *not* the original file,
   so you keep an unmodified backup) → test in-game or in a VEXX viewer.

## 3. What the add-on understands

WipEout Pure/Pulse tracks are stored in the **VEXX** binary container format. The
add-on understands a few specific node types well enough to edit them:

- **Collision nodes** (`collision_floor`, `collision_wall`, `collision_reset`,
  and similar) — the invisible geometry used for physics. Positions are stored as
  plain 32-bit floats.
- **Mesh nodes** (VEXX signature `0x11e`) — the *visible* track and scenery
  geometry, as rendered by the game. Positions are quantized as 16-bit integers
  plus a scale factor (per mesh "chunk"), in the PSP GPU's native vertex format,
  arranged as triangle strips.
- **Pad nodes** (`speedup_pad_nolightparentShape`, signature `0x36f`, and
  `weapon_pad_nolightparentShape`, signature `0x370`) — the speed-boost and
  weapon pickup pads. These are subclasses of the same mesh format above
  (identical binary layout, just a different outer node signature), so they're
  handled by the exact same parser. Each pad's geometry is defined once, in
  local/template space, and instanced at its real track position/orientation via
  a separate wrapper node holding a 4x4 transform matrix. This matters for
  gameplay, not just looks: the game reads the wrapper's transform (not the mesh
  geometry) to decide where a pad actually triggers, so the add-on exposes it as
  the pad *object's own* Blender transform rather than baking it into the mesh —
  moving/rotating the pad object in Object Mode edits the wrapper directly, so
  the pad also works in the new spot in-game, not just visually in Blender. The
  object's origin is placed at the pad's own geometric center (not wherever local
  (0,0,0) in the raw template happens to be), so rotating it in Object Mode
  pivots around the pad itself instead of some arbitrary off-center point.
- **Texture nodes** (VEXX signature `0x373`) — read-only, for display. The add-on
  decodes the game's actual in-game textures (indexed/paletted color, with the
  PSP's texture-memory "swizzle" layout un-scrambled where used) and UV
  coordinates, and builds a real Blender material per texture actually referenced
  by the meshes you import. This makes it much easier to tell what you're looking
  at while editing (e.g. distinguishing a road surface from a wall from a
  barrier) instead of a flat grey mesh. Texture import can be turned off in the
  import dialog for a faster, geometry-only import.
- **The racing line** (`WO_TRACK` node, signature `0x36D`) — the path the AI
  follows. The add-on reads it to build the editable lane lines and to drive the
  AI path Forward/Reverse toggle (see §7).

## 4. Import

`VEX Tools panel → Import VEX Track` reads a `.vex` file and builds Blender
objects, organized under a **`VEX Track`** collection so your Scene Collection
stays tidy:

- `VEX - Collisions Floor`
- `VEX - Collisions Wall`
- `VEX - Collisions Reset`
- `VEX - Collisions Other` (non-standard collision nodes that don't fit the usual
  structure — see "Known limitations")
- `VEX - Track Mesh` — used when the file's mesh nodes follow the `Track_Shape*`
  naming convention (one merged object)
- `VEX - Track Surface (guessed)` / `VEX - Landscape (guessed)` — every mesh-type
  node NOT covered by the `Track_Shape*` naming convention above (e.g. plain Maya
  export names like `polySurfaceShape1825`, or partial conventions like
  `Track_40Shape` / `nolight_Shape`), imported as separate objects and sorted by
  a simple name heuristic ("does the name contain 'track'?"). This step always
  runs, regardless of whether `Track_Shape*` nodes were also found — some real
  files mix both naming styles for different parts of the same track, and
  skipping this step whenever any `Track_Shape*` node existed used to silently
  drop that geometry entirely (fixed). This is a guess, not a certainty — feel
  free to drag objects between the two collections; it has no effect on export
  correctness either way.
- `VEX - Pads (Speedup)` / `VEX - Pads (Weapon)` — speed-boost pads and weapon
  pickup pads, each in their own collection (unlike the reference VS Code
  extension, which groups both under a single undifferentiated "Pads" toggle).
- `VEX - WO Track Lanes` — the racing-line lane lines (see §7).

Each imported object carries hidden custom properties (`vex_fmt`, `vex_offsets`,
`vex_scales`, `vex_orig_vcount`) that record exactly where in the original file
each of its vertices came from. These are what make export possible.

## 5. Edit

Move vertices around like any other Blender mesh. That's it — that's the whole
supported editing operation. See "What NOT to do" below for why this is a hard
boundary, not just a suggestion.

## 6. Export

`VEX Tools panel → Export VEX Track` re-opens the *original* source file (the one
you imported from — its path is remembered automatically), and for every object
in the scene that carries VEX metadata, writes its current vertex positions back
into a copy of that file, at the exact original byte offsets. Everything else in
the file is left untouched. You choose the output path, so the original file is
never overwritten unless you explicitly pick the same name.

### How export works internally

- Every vertex, at import time, keeps a record of the exact byte offset (and, for
  mesh nodes, the per-chunk quantization scale) it was read from.
- At export time, the add-on never regenerates or reinterprets the file structure.
  It opens the *original* bytes fresh and overwrites only those specific byte
  ranges with the current (possibly edited) vertex positions, converting back from
  Blender's coordinate space and, for mesh nodes, re-quantizing to 16-bit integers
  using the recorded scale.
- **Axis convention:** the game uses Y-up, Blender uses Z-up. Import/export apply
  an exact, mutually-inverse conversion (`(x,y,z) → (x,-z,y)` on import, its
  inverse on export) so edits round-trip correctly and the track appears upright
  in Blender's viewport.

This design means the add-on doesn't need to fully understand triangle/index
data, materials, textures, or the dozens of other node types in a `.vex` file —
it only needs to find vertex positions reliably, which has been verified by
re-deriving files with known, real, manually-made edits and confirming the result
is byte-for-byte identical to those edits.

## 7. Racing-line tools (lane lines & AI path)

In addition to geometry import/export, the add-on provides tools that work with
the **racing line** — the `WO_TRACK` node (`wotrack_`, signature `0x36D`) that
the AI follows.

### Editable lane lines

On import, the add-on builds one **editable mesh object per lane**
(`WO_Track_Lane_0`, `WO_Track_Lane_1`, …) under the `VEX - WO Track Lanes`
collection, colored differently, from the `WO_TRACK` racing-line points.

Unlike a curve, this is a **real mesh whose vertices map 1:1 (by index)** to the
position bytes (`+0x00`) of the `WO_TRACK` points. Each vertex carries a
`wotrack_off` custom property = the file byte offset of that point's position, so:

- **Moving a vertex in Edit Mode and exporting writes the new position back into
  the `.vex` file.** This is how you fix spots where the AI hugs the wall — you
  nudge the racing line at that point and re-export.
- **You can fine-tune the racing line manually** by translating points in **Edit
  Mode** (select one or more vertices and move them) or by moving/rotating the
  whole lane object in **Object Mode** (`G`/`R`/`S`). Export reads each vertex's
  final world-space position through the object's transform, so both approaches
  round-trip correctly. You must keep the **same number of vertices** (no
  add/remove) — only translation is supported.
- The lane lines are **visual, not physical**: they don't change the ship's
  collision, only the path the AI steers toward.

### AI path toggle (Forward ↔ Reverse)

The **`Toggle AI Path`** button in the panel is a **clickable box** that shows
`path: Forward` by default and flips to `path: Reverse` when clicked (and back
again on a second click). It inverts the AI racing line in the imported track
file. It works by calling the same `reverse_wotrack_payload()` routine described
in Appendix A (§A.5). Concretely, it:

1. Reads the source `.vex` file fresh from disk.
2. Locates the `WO_TRACK` node and **reverses the points inside each lane,
   without swapping the lane order**, keeping the lane descriptors and lane graph
   unchanged.
3. Negates `forward`, keeps `down`, recomputes `right = forward × down`,
   swaps `leftMetric`/`rightMetric`, and rebuilds the monotonic `param`.
4. Writes the modified payload back to the same source file.
5. Toggles a scene flag (`vex_ai_reverse`) so the button label flips between
   `path: Forward` and `path: Reverse`.
6. Re-imports the file to refresh the lane visualization and geometry in the
   viewport (best-effort; the file on disk is already correct).

> **Note:** this toggle **overwrites the source file** on disk with the reversed
> (or re-reversed) racing line. Keep a backup of the original if you need the
> vanilla path back.

### WO Track Lanes toggle

The **`Toggle WO Track Lanes`** button shows/hides the `VEX - WO Track Lanes`
collection (viewport + render). Useful when the lane lines clutter the view while
you work on the track mesh.

## 8. What NOT to do

The add-on works by matching vertices **1:1, by index order**, against a list of
byte offsets recorded at import time. Anything that changes vertex *count* or
*order* silently breaks that correspondence — the wrong bytes would get written
to the wrong places. To keep exports safe:

- **Never add or remove vertices.** No Extrude, Merge, Delete, Subdivide, Loop
  Cut, Knife, Bridge Edge Loops, Bisect, Boolean modifiers, Decimate, Remesh, or
  anything else that changes topology. Moving is the only supported edit.
- **Never merge/join two VEX objects together** (`Ctrl+J`). This mixes vertex
  data from two different byte-offset mappings into one mesh and will corrupt the
  export for both.
- **Moving/rotating a whole object (Object Mode, `G`/`R`/`S`) is safe and
  correctly exported.** Export reads each vertex's final world-space position —
  through the object's transform — not just its raw mesh-local coordinates, so a
  rigid move/rotate/scale of an entire object is equivalent to moving all its
  vertices together and works exactly as expected. This general rule has one
  exception: pads (see above) instead expose the wrapper's own transform as the
  object's transform, since for those the object-level transform *is* the thing
  that needs editing for gameplay purposes.
- **Be careful with duplicating objects.** Duplicating a VEX object (e.g.
  `Shift+D`) copies its custom properties too, so you can end up with two objects
  that both claim to represent the *same* source vertices. On export, whichever
  one is processed last silently overwrites the other's changes at those shared
  byte offsets — with no error, because nothing is technically invalid. If you
  don't need a duplicate, delete it before exporting. If you accidentally imported
  the same file twice in one session, you'll have `.001`-suffixed duplicates of
  everything; delete the unwanted set.
- **Deleting a whole unwanted object is safe.** Unlike editing, removing an entire
  object (e.g. a building you don't care about, in the fallback "Landscape"
  collection) is fine: export simply won't touch that geometry, leaving it exactly
  as it was in the source file.
- **Exit Edit Mode before exporting.** The add-on now does this automatically as a
  safety net, but it's good practice regardless. While in Edit Mode, Blender keeps
  your edits in a working copy (BMesh) that isn't reflected on the underlying mesh
  data until you leave Edit Mode — reading vertex positions before that happens
  returns stale, pre-edit values.
- **Keep a backup of the original file.** Export always reads from the *original*
  file path remembered at import time and writes a new file at the path you
  choose — it never modifies the original in place unless you deliberately export
  to the same filename. Still, keep a separate backup; a wrong file selected in
  the export dialog is an easy mistake to make.

## 9. Known limitations

- **Not every node parses.** A handful of node types look superficially like
  standard collision or mesh nodes but aren't (e.g. some `collision_wall_*`
  variants turned out to be transform/instance nodes referencing shared geometry
  elsewhere, not embedded collision data). These are detected and skipped safely,
  with a warning, rather than producing garbage data.
- **Some collision-named nodes are empty by design.** Certain tracks contain
  plain, childless transform nodes under collision-suggestive names (e.g.
  `collision_wall_freestyleN`) that hold no geometry at all — just a 4x4 matrix
  with nothing attached. These appear to be leftover Maya authoring locators,
  since the game engine dispatches behavior by node type, not by name, and would
  treat them as ordinary empty transforms. The add-on distinguishes these from
  genuine parsing failures and reports them separately (as an informational note,
  not a warning) — skipping them loses no in-game collision data.
- **The track vs. landscape split (fallback mode) is a naming guess.** Files
  without a `Track_Shape*` naming convention have no reliable way — from the
  binary data alone — to distinguish the drivable surface from scenery.
  Double-check the split and move objects between the two collections as needed.
  Note that this split is applied per-node, not per-file: a file can (and some
  real ones do) mix `Track_Shape*`-named pieces with generically-named ones for
  different parts of the same track, and both are imported correctly.
- **Triangle strip assembly is a reconstruction, not a byte-exact port** of the
  original renderer's logic for every edge case (e.g. multi-section chunks, which
  haven't been observed in practice so far). This only affects how the mesh
  *looks* in Blender — export never writes topology data, only positions, so it
  cannot corrupt the file even if a specific strip's reconstructed shape isn't
  pixel-perfect.
- **External/shared mesh references aren't resolved.** Some mesh nodes don't embed
  geometry at all — they reference geometry defined elsewhere in the file (or in
  another file entirely). These are detected and skipped rather than guessed at.
- **This add-on has only been tested on WipEout Pure (VEXX version 4) files.**
  WipEout Pulse (version 6) uses a slightly different stride layout in places;
  partial support exists in the parsing code but it is untested.
- **Textures are for display only and are never written back.** Editing or
  replacing a texture image in Blender has no effect on export — only vertex
  positions round-trip back to the `.vex` file. Only the first (largest) mipmap
  level of each texture is decoded; smaller mipmap levels are ignored since they
  aren't needed for a static preview. A texture whose material can't be resolved
  (rare — e.g. a chunk's material index or texture id doesn't match anything
  found) is silently left untextured rather than blocking the import of its
  geometry.
- **The lane lines and AI-path toggle are for WipEout Pure (VEXX v4) tracks.**
  The `WO_TRACK` parser, the editable lane mesh, and the `reverse_wotrack_payload`
  routine all assume the Pure stride layout (112-byte points). They have not been
  validated against Pulse (v6).

## 10. Credits

This add-on would not exist without prior reverse-engineering work by others:

- **[PierreBelmondo/vscode-wipeout](https://github.com/PierreBelmondo/vscode-wipeout)**
  (MIT license) — a VS Code extension for WipEout modding. Its TypeScript source
  (`core/formats/vexx/v4/collision.ts`, `mesh.ts`, `texture.ts`, `speedup_pad.ts`,
  `weapon_pad.ts`, `core/formats/vexx/index.ts`, `core/utils/pspgu.ts`,
  `core/primitive/aabb.ts`, `primitive/mesh.ts`) is the ground truth this add-on's
  binary parsers are ported from. Without access to that source, the collision,
  mesh, pad, and texture formats used here would only be approximate.
- **[thp.io](https://thp.io) (Thomas Perl)** — original public reverse engineering
  of the outer VEXX node-tree container format (`walk-vex.py`, 2022), which this
  add-on's tree walker is based on.
- The PSP homebrew community's documentation of the `sceGu` vertex format
  (`GU_VERTEX_8/16/32BIT`, etc.), which the mesh nodes use internally for
  quantized vertex storage.
- **droastedcat** — maintainer, and the one who did all the real-world testing (in
  Blender and in-game) that found and drove the fixes for every bug described in
  this document.

If you build on this add-on, please keep crediting the sources above — particularly
PierreBelmondo's project, which supplied the actual format specification.

---

# Appendix A — The AI / racing system

This appendix documents the **racing system** of WipEout Pure (PSP, EUR version
`UCES-00001`) and the process used to **reverse the AI**. It is included here as
technical reference for anyone working with the racing line in the Blender
add-on (the editable lane lines and the AI-path toggle in §7).

## A.1 Track file structure (`.vex`)

Every track is a `.vex` file (the **VEXX** container format, version 4 on PSP).
A single file is a **tree of typed nodes** that contains everything needed for
the track environment: geometry, collision, lights, sounds, cameras and — most
importantly — **the racing path**.

### VEXX header (16 bytes)

```c
struct VexxHeader {
    uint32_t version;         // Pure on PSP == 4
    uint32_t part1_length;    // length of part 1 (node tree)
    uint32_t part2_length;    // length of part 2 (textures)
    char     magic[4];        // "VEXX"
};
```

After the header, part 1 (the node tree) and then part 2 (textures). Nodes have a
16-byte header:

```c
struct TreeNodeHeader {
    uint32_t signature;   // node type
    uint16_t header_len;  // header + name length
    uint16_t unk1;
    uint32_t payload_len; // payload length
    uint16_t nchildren;   // number of children
    uint16_t unk2;
};
```

The node name is an ASCII null-terminated string immediately after the header.

### Nodes relevant to the racing system

| Node | Signature | Role |
|------|-----------|------|
| `wotrack_` | `0x36D` | **The racing path / racing line.** Defines the path the AI follows and the lap progress. |
| `wopoint*` / `wopointShape*` | `0x6D` / `0x381` | Navigation waypoints (points in space with corridor dimensions/direction). |
| `collision_floor` | `0x36B` | Floor collision geometry (what ships can drive on). |
| `collision_wall` | `0x36C` | Wall collision geometry. |
| `collision_reset` | `0x37F` | Reset zones. |
| `Start_Position_1` | `0x36E` | Starting grid position. |
| `starting_line_*` | `0x6D` | Start/finish line. |
| `Section*` | `0x37B` | Track sections (reference segments). |
| `Track_Shape*` | `0x11E` | Visible track mesh. |

## A.2 The `wotrack_` node

The `wotrack_` node (signature `0x36D`, magic `WOtd` = `0x574F7464`) is **the
racing path**. It is a **continuous loop** made of one or more **lanes**
consecutive to each other.

### Payload structure

```
+0x00  magic u32 = 'WOtd' (0x574F7464)
+0x04  sectionCount u32
+0x08  laneCount u32
+0x0C  laneGraphRowCount u32
+0x10  _unknown (48 bytes, not yet decoded)
+0x40  lane table (laneCount * 32 bytes)
+...   lane graph (laneGraphRowCount * 16 bytes)
+...   points (totalPoints * 112 bytes)
```

### Lane descriptor (32 bytes each)

```
+0x00  pointCount u32        // number of points in this lane
+0x04  scale f32             // lane scale / average width
+0x08  _unknown0 u32         // (always 0)
+0x0C  nextLaneGraphIdx u32  // lane-graph index for the "next" connection
+0x10  prevLaneGraphIdx u32  // lane-graph index for the "prev" connection
+0x14  _unknown1..3 u32      // (always 0)
```

### Track point (`WoTrackPointV4`, 112 bytes)

```
+0x00  position (4f)   // world-space lane-centre position (xyz, w=0)
+0x10  right    (4f)   // lateral (side) vector, unit
+0x20  down     (4f)   // downward (surface) vector, unit = -normal
+0x30  forward  (4f)   // direction of travel, unit   <-- KEY for reverse
+0x40  param    (f32)  // arc-length parameter (monotonically increasing ~0..1)
+0x44  leftMetric (f32)// distance to left border
+0x48  rightMetric(f32)// distance to right border
+0x4C  _unknown0 (f32)
+0x50  _unknown1 (f32)
+0x54  tailMeta (28 bytes)
```

### Fundamental vector properties

The three vectors `right`, `down`, `forward` must form an **orthonormal basis**:

- `right = forward × down`  (cross product)
- `down = -surfaceNormal`  (the ship's up is `-down`)
- All three are **unit vectors** (magnitude ~1.0)

The validation tool (from `PierreBelmondo/vscode-wipeout`) checks that each vector
has magnitude ~1.0 and forms an orthonormal basis. **If this property breaks, the
AI ship points the wrong way and visual bugs appear (lights/orientation).**

## A.3 How the AI uses the `wotrack_`

The AI (and the race-progress system) uses the `wotrack_` node to:

1. **Determine the path** to follow (the racing line).
2. **Compute lap progress** via the `param` (arc-length).
3. **Count laps** when the `param` "wraps" (goes from ~1 to 0).
4. **Switch lanes** via the **lane graph** (the `next`/`prev` connections).

The `forward` of each point is the **direction of travel**. When the ship follows
the path, it orients along the `forward` of the current point.

> **Important note:** in this project we verified that, for WipEout Pure, the AI
> uses the **`wotrack_` node** for navigation (not the `wopoint*`). Indeed, by
> inverting only the `wotrack_`, the ships run in reverse **without** having to
> touch the `wopoint`.

## A.4 How to successfully reverse the AI

After many attempts, here is the correct solution to reverse the path.

### A.4.1 Prerequisite: a track drivable in reverse

The base file **must** be a track that can be driven in both directions. Some
vanilla tracks have **jumps/ramps** that are not drivable in reverse. In this
project we used a modified file (`modmesh3.vex`) in which the **collisions**
(`collision_floor`, `collision_wall`) and the **mesh** were modified to make the
jump drivable in reverse.

> ⚠️ The `wotrack_` node of the modified file is identical to the vanilla one.
> Only collisions/mesh differ.

### A.4.2 The solution: reverse the points *inside* each lane, WITHOUT swapping lanes

**The key step that solved the problem** was:

1. **Reverse the point order WITHIN each lane** (the path flows the other way).
2. **Do NOT swap the lane order** (keep lane[0] with its pointCount, lane[1] with
   its own).
3. **Leave unchanged** the lane descriptors (`next`/`prev`) and the **lane graph**.

Why this matters:
- If you **swap the lanes**, the **lane graph** becomes incoherent (it points to
  lanes with wrong pointCounts) → the AI, at the lane transition, goes to the
  wrong lane → **hits the wall**.
- If you **only reverse the points inside each lane** and keep the lane structure,
  the lane graph stays valid → the AI correctly transitions from lane[0] to
  lane[1].

### A.4.3 Vector transformation (keeping the orthonormal basis)

For each point, after reversing the order:

```python
forward_new = -forward_old   # direction of travel inverted
down_new    = down_old       # surface unchanged
right_new   = normalize(forward_new × down_new)  # orthonormal basis preserved
```

Additionally:
- **Swap `leftMetric` and `rightMetric`** (the borders invert in reverse).

> ⚠️ **Caution:** swapping `leftMetric`/`rightMetric` should only be done **if
> necessary**. In our case the wall-hitting problem at a specific point was caused
> by **swapping the lanes**, not by swapping the metrics. The final variant
> **does not swap the lanes** and keeps the metrics coherent.

### A.4.4 Rebuild the `param`

The `param` must be **monotonically increasing** along the new direction of
travel. It must be rebuilt as **normalised arc-length** (cumulative distance
between consecutive points, excluding any "crossovers" — anomalous distances
> 10× the median, which are circuit teleports and not real length).

```python
# distances between consecutive points (closed loop)
# exclude distances > 10 * median (crossovers)
# param[i] = cumulative / total
```

### A.4.5 Start position (optional, for spawn)

If you also need correct spawning, the start position (`Start_Position_1`) must
be moved to the `wotrack_` point with `param=0` (the start of the reverse lap).
In this project, however, the vanilla spawn already worked, so this was not
necessary.

## A.5 The reverse algorithm

The reverse routine (`reverse_wotrack_payload()`, also exposed as the AI-path
toggle in §7) does:

1. Reverses the points **inside each lane** (without swapping lane order).
2. Keeps **unchanged** the lane descriptors and the lane graph.
3. Negates `forward`, keeps `down`, recomputes `right = forward × down`.
4. Swaps `leftMetric`/`rightMetric`.
5. Rebuilds the monotonic `param`.

### Validation

The resulting file must pass these checks:
- Vectors `right`, `down`, `forward` are **unit** (magnitude ~1.0).
- `right = forward × down` (orthonormal basis).
- `param` **monotonically increasing** (0 jumps > 0.05).
- The `.vex` file has **the same size** as the original (surgical patch, not a
  rebuild).

## A.6 Troubleshooting

| Symptom | Cause | Solution |
|---------|-------|----------|
| Ships hit the wall at a specific point (at the lane transition) | **Lanes swapped** → incoherent lane graph | Do NOT swap the lane order |
| Visual bugs (lights/orientation) on all ships | Broken orthonormal basis (`right`, `down`, `forward` incoherent) | Recomputed `right = forward × down`, keep `down` |
| Ships run forward instead of reverse | `forward` not negated | Negate `forward` |
| Ships penetrate walls | Wrong navigation direction | Reverse the points inside each lane |
| Wrong lap counter (double increment) | `param` not monotonic | Rebuild `param` as arc-length |

## A.7 The Quake deformation (known limitation)

The **Quake** weapon generates a seismic wave that travels along the circuit,
producing two effects:

- **Damage** (game logic): **works correctly in reverse** ✓
- **Visual deformation** (particle system): **remains forward** ✗ (known limitation)

### Why

The `Quake` node (signature `0x379`, name `Quake`, a `Quake_ImportNode`) **does
not contain the path** — it imports the particle systems `WO_QUAKE.POB` /
`WO_TRACK_ROCK_DEBRIS.POB` from `Data\psys\`. The **direction of the deformation
is computed at runtime** by the code (`~QUAKETRAVEL`), not by the track file.

### Quake node structure (payload, 249 records of 96 bytes)

```
+0x00  count u32 (= 249)  // number of records
+0x1C  link_a  (u16)      // structural linked list (next) — DO NOT invert
+0x1E  sentinel -1
+0x20  link_b  (u16)      // second linked list — DO NOT invert
+0x22  sentinel -1
+0x2A  record index
+0x30  offset (u16)
+0x32  branch (u16, 0/1/2)
+0x38  count (u16)
+0x3C  t0 (f32)  // only meaningful progress field, read by the code
+0x40  t1 (f32)  // interval end (t0 <= t1)
```

### What does NOT work (tested and discarded)

| Variant | Modification | Result |
|---------|--------------|--------|
| `a` | Invert `link_a` | ❌ lag + irregular waves (link is structural) |
| `ab` | Invert `link_a` + `link_b` | ❌ lag + irregular waves (same as `a`) |
| `t` | Mirror `t0`/`t1` | ❌ no effect on direction |
| `t0` / `t1` | Mirror a single field | ❌ structurally invalid (breaks `t0 <= t1`) |

### Conclusion

- `link_a`/`link_b` are the **structural pointers** of the particle linked list:
  inverting them corrupts the system.
- The deformation direction is **not** in the header records (where `+0x3C`
  lives), but in the **per-particle data** in the geometry region (offset
  `249*96 = 23904`, ~171808 bytes), which begins with a **separate table of 2832
  values in [0,1]** (per-particle animation curves).
- Modifying the Quake node in the `.vex` alone **did not** fix the direction.

> **Accepted limitation:** the visual Quake deformation remains forward. The
> **damage** (gameplay) and the **red lights** work correctly in reverse. The fix
> would require a patch to the runtime code (`~QUAKETRAVEL`) — not performed for
> stability reasons.

---

# References

- **VEXX format** — `thp.io/2022/vexx-file-format.html` (Thomas Perl, `walk-vex.py`)
- **WO_TRACK parser** — `PierreBelmondo/vscode-wipeout` (`core/formats/vexx/v4/wo_track.ts`)
- **WAD tool** — `p.py` (wadutil) to extract/recreate the `Data.wad`
