bl_info = {
    "name": "WipEout VEX Track Tools",
    "author": "Claude + droastedcat + community reverse engineering (PierreBelmondo/vscode-wipeout)",
    "version": (1, 1, 0),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > VEX Tools",
    "description": "Import/export WipEout Pure/Pulse track collision and mesh geometry (.vex)",
    "category": "Import-Export",
}

import bpy
import struct
import json
import traceback
from mathutils import Vector
from bpy.props import StringProperty
from bpy_extras.io_utils import ImportHelper, ExportHelper


# ============================================================================
# BINARY PARSING - direct port of the validated standalone Python tools.
# Roundtrip correctness confirmed byte-for-byte against real in-game edits.
# ============================================================================

# ---- VEXX node tree (format reverse-engineered by thp.io, "walk-vex.py") ----

def vexx_parse_tree(data):
    offset = 0

    def eat(fmt):
        nonlocal offset
        size = struct.calcsize(fmt)
        vals = struct.unpack_from(fmt, data, offset)
        offset += size
        return vals

    version, part1_length, part2_length, magic = eat('<III4s')

    def parse_one():
        nonlocal offset
        start_offset = offset
        signature, header_len, unk1, payload_len, nchildren, unk2 = eat('<IHHIHH')
        namedata = data[offset:offset + header_len - 16]
        offset += header_len - 16
        payload_offset = offset
        offset += payload_len
        name = namedata.split(b'\0', 1)[0].decode(errors='replace')
        node = dict(signature=signature, name=name, start=start_offset,
                    poff=payload_offset, plen=payload_len, children=[])
        for _ in range(nchildren):
            node['children'].append(parse_one())
        return node

    root = parse_one()
    return root


def vexx_flatten(node, acc=None):
    if acc is None:
        acc = []
    acc.append(node)
    for c in node['children']:
        vexx_flatten(c, acc)
    return acc


# ---- collision nodes (core/formats/vexx/v4/collision.ts) ----

class NotACollisionNode(Exception):
    pass


def _parse_points(data, offset):
    type_ = struct.unpack_from('<I', data, offset)[0]
    point_size = struct.unpack_from('<H', data, offset + 4)[0]
    points_count = struct.unpack_from('<H', data, offset + 6)[0]
    total_size = 8 + point_size * points_count
    entry = dict(type=type_, point_size=point_size, points_count=points_count,
                 data_offset=offset + 8, size=total_size)
    if type_ == 1:
        entry['vertices'] = [struct.unpack_from('<3f', data, offset + 8 + i * 12)
                              for i in range(points_count)]
    elif type_ == 2:
        entry['triangles'] = [struct.unpack_from('<3H', data, offset + 8 + i * 6)
                               for i in range(points_count)]
    return entry


def _parse_block(data, offset):
    block_count = struct.unpack_from('<I', data, offset)[0]
    points_list = []
    o = offset + 4
    for _ in range(block_count):
        p = _parse_points(data, o)
        points_list.append(p)
        o += p['size']
    return dict(points=points_list, size=o - offset)


def parse_collision_node(data, poff, plen):
    signature = struct.unpack_from('<I', data, poff)[0]
    block_count = struct.unpack_from('<I', data, poff + 4)[0]
    if block_count > plen // 8:
        raise NotACollisionNode(f"implausible block_count={block_count} for plen={plen}")
    blocks = []
    o = poff + 8
    for _ in range(block_count):
        if o + 4 > poff + plen:
            raise NotACollisionNode("read past end of payload")
        b = _parse_block(data, o)
        blocks.append(b)
        o += b['size']
    return dict(signature=signature, blocks=blocks)


def iter_collision_vertex_blocks(parsed_node):
    for block in parsed_node['blocks']:
        vtx_points = next((p for p in block['points'] if p['type'] == 1), None)
        idx_points = next((p for p in block['points'] if p['type'] == 2), None)
        if vtx_points is None:
            continue
        vertices = []
        for i, (x, y, z) in enumerate(vtx_points['vertices']):
            vertices.append(dict(x=x, y=y, z=z, file_offset=vtx_points['data_offset'] + i * 12))
        triangles = idx_points['triangles'] if idx_points else []
        yield vertices, triangles


# ---- PSP GU vertex format (core/utils/pspgu.ts) ----

class PrimitiveType:
    TRIANGLE_STRIP = 4


def _gu_stride_info(vtxdef):
    TEXTURE_BITS = 0x3
    COLOR_BITS = 0x7 << 2
    NORMAL_BITS = 0x3 << 5
    VERTEX_BITS = 0x3 << 7
    VERTICES_BITS = 0x7 << 18

    texture_bits = (vtxdef & TEXTURE_BITS) >> 0
    texture_size = 0 if texture_bits == 0 else (4 if texture_bits == 3 else texture_bits)
    texture = dict(size=texture_size, offset=0, count=2)
    texture_end = texture['offset'] + texture['size'] * texture['count']

    color_bits = (vtxdef & COLOR_BITS) >> 2
    color_size = 0 if color_bits == 0 else (4 if color_bits == 7 else 2)
    color_padding = 0
    if color_size > 1:
        color_padding = 0 if texture_end % color_size == 0 else color_size - (texture_end % color_size)
    color = dict(size=color_size, offset=texture_end + color_padding, count=1)
    color_end = color['offset'] + color['size'] * color['count']

    normal_bits = (vtxdef & NORMAL_BITS) >> 5
    normal_size = 0 if normal_bits == 0 else (4 if normal_bits == 3 else normal_bits)
    normal_padding = 0
    if normal_size > 1:
        normal_padding = 0 if color_end % normal_size == 0 else normal_size - (color_end % normal_size)
    normal = dict(size=normal_size, offset=color_end + normal_padding, count=3)
    normal_end = normal['offset'] + normal['size'] * normal['count']

    vertex_bits = (vtxdef & VERTEX_BITS) >> 7
    vertex_size = 0 if vertex_bits == 0 else (4 if vertex_bits == 3 else vertex_bits)
    vertex_count_field = (vtxdef & VERTICES_BITS) >> 18
    vertex_padding = 0
    if vertex_size > 1:
        vertex_padding = 0 if normal_end % vertex_size == 0 else vertex_size - (normal_end % vertex_size)
    vertex_count = vertex_count_field if vertex_count_field != 0 else (0 if vertex_size == 0 else 3)
    vertex = dict(size=vertex_size, offset=normal_end + vertex_padding, count=vertex_count)

    return dict(texture=texture, color=color, normal=normal, vertex=vertex)


def _gu_stride_size(vtxdef, align):
    si = _gu_stride_info(vtxdef)
    size = si['vertex']['offset'] + si['vertex']['size'] * si['vertex']['count']
    if align > 1 and size % align != 0:
        size += align - (size % align)
    return size


# ---- mesh nodes (core/formats/vexx/v4/mesh.ts) ----

def _u16(data, off): return struct.unpack_from('<H', data, off)[0]
def _u8(data, off): return data[off]


def _parse_mesh_header(data, poff):
    mesh_count = _u16(data, poff + 2)
    length1 = struct.unpack_from('<I', data, poff + 4)[0]
    length2 = struct.unpack_from('<I', data, poff + 8)[0]
    chunk_start = length2 if length2 else length1
    return dict(mesh_count=mesh_count, size=48, chunk_start=chunk_start)


def _parse_chunk_header(data, off):
    stride_count1 = _u16(data, off + 4)
    stride_count2 = _u16(data, off + 6)
    primitive_type = _u8(data, off + 8)
    vtxdef = _u16(data, off + 10)
    size1 = _u16(data, off + 12)
    id_ = _u8(data, off + 2)
    return dict(id=id_, stride_count1=stride_count1, stride_count2=stride_count2,
                primitive_type=primitive_type, vtxdef=vtxdef, size1=size1)


def _parse_chunk(data, off, n_materials, version=4):
    header = _parse_chunk_header(data, off)
    if header['vtxdef'] == 0:
        return None, 0

    si = _gu_stride_info(header['vtxdef'])
    stride_align = 4 if version < 6 else max(
        si['texture']['size'], si['color']['size'], si['normal']['size'], si['vertex']['size'], 1)
    ss = _gu_stride_size(header['vtxdef'], stride_align)

    def compute_size(ss_):
        s = 16 + 48
        s += header['stride_count1'] * ss_
        if s % 16 != 0:
            s += 16 - (s % 16)
        s += header['stride_count2'] * ss_
        if s % 16 != 0:
            s += 16 - (s % 16)
        return s

    size = compute_size(ss)
    if header['id'] >= n_materials:
        return None, 0

    info_off = off + 16
    scaling = 1.0
    if si['vertex']['size'] == 2:
        scaling = struct.unpack_from('<f', data, info_off)[0]

    strides_off = info_off + 48
    sec1_end_rel = 16 + 48 + header['stride_count1'] * ss
    pad = 0 if sec1_end_rel % 16 == 0 else 16 - (sec1_end_rel % 16)
    strides2_off = off + sec1_end_rel + pad

    chunk = dict(header=header, offset=off, size=size, stride_info=si, stride_size=ss,
                 scaling=scaling, strides_off=strides_off, strides2_off=strides2_off)
    return chunk, size


def parse_mesh_node(data, poff, plen, version=4, max_chunks=100):
    header = _parse_mesh_header(data, poff)
    chunk_start = header['chunk_start']

    if chunk_start == 0 or poff + chunk_start + 16 > poff + plen or _u16(data, poff + chunk_start + 10) == 0:
        return dict(external=True, chunks=[])

    n_materials = header['mesh_count']
    chunks = []
    coff = poff + chunk_start
    end = poff + plen
    while coff + 16 <= end and len(chunks) <= max_chunks:
        if _u16(data, coff + 10) == 0:
            break
        chunk, size = _parse_chunk(data, coff, n_materials, version=version)
        if chunk is None or size == 0:
            break
        chunks.append(chunk)
        coff += chunk['size']

    return dict(external=False, chunks=chunks)


def extract_chunk_vertices(data, chunk, section=1):
    si = chunk['stride_info']
    vs = si['vertex']['size']
    vo = si['vertex']['offset']
    ss = chunk['stride_size']
    if section == 1:
        n = chunk['header']['stride_count1']
        base_off = chunk['strides_off']
    else:
        n = chunk['header']['stride_count2']
        base_off = chunk['strides2_off']
    verts = []
    if vs != 2:
        return verts
    scale = chunk['scaling'] / 32767.0
    for i in range(n):
        rec_off = base_off + i * ss + vo
        rx, ry, rz = struct.unpack_from('<3h', data, rec_off)
        if rx in (32767, -32768) or ry in (32767, -32768) or rz in (32767, -32768):
            verts.append(None)
            continue
        verts.append(dict(x=rx * scale, y=ry * scale, z=rz * scale, file_offset=rec_off))
    return verts


def pack_mesh_data(sections, triangle_strip=True):
    out_verts = []
    triangles = []
    run = 0
    for si, sec in enumerate(sections):
        if si > 0:
            run = 0
        for v in sec:
            if v is None:
                run = 0
                continue
            out_verts.append(v)
            idx = len(out_verts) - 1
            run += 1
            if triangle_strip:
                if run >= 3:
                    a, b, c = idx - 2, idx - 1, idx
                    if (run - 3) % 2 == 0:
                        triangles.append((a, b, c))
                    else:
                        triangles.append((b, a, c))
            else:
                if run % 3 == 0:
                    triangles.append((idx - 2, idx - 1, idx))
    return out_verts, triangles


# ============================================================================
# Axis conversion: game (Y = up) <-> Blender (Z = up)
# import and export MUST be exact inverses of one another
# ============================================================================

def game_to_blender(x, y, z):
    return (x, -z, y)


def blender_to_game(x, y, z):
    return (x, z, -y)


def category_of(name):
    low = name.lower()
    if low.startswith('collision_floor'):
        return 'collisions_floor'
    if low.startswith('collision_wall'):
        return 'collisions_wall'
    if low.startswith('collision_reset'):
        return 'collisions_reset'
    return 'collisions_other'


def looks_like_track_surface(name):
    """Best-effort heuristic for the signature-based fallback: nodes whose
    name contains 'track' are assumed to be part of the drivable surface,
    everything else is treated as landscape/scenery. This is a guess, not
    a certainty - feel free to drag objects between the two collections
    in Blender afterwards, it has no effect on export correctness."""
    return 'track' in name.lower()


def get_or_create_collection(name, parent_collection):
    """Returns an existing collection by name, or creates and links a new
    one under parent_collection. Reused across multiple imports in the
    same Blender session, so importing a second file adds into the same
    folders rather than creating duplicates."""
    if name in bpy.data.collections:
        coll = bpy.data.collections[name]
    else:
        coll = bpy.data.collections.new(name)
    if not any(c.name == coll.name for c in parent_collection.children):
        parent_collection.children.link(coll)
    return coll


def setup_vex_collections(context):
    """Creates the 'VEX Track' collection tree used to organize imported
    objects, so the Scene Collection itself stays tidy (one entry to
    expand, instead of dozens/hundreds of loose objects)."""
    root = get_or_create_collection("VEX Track", context.scene.collection)
    return dict(
        root=root,
        collisions_floor=get_or_create_collection("VEX - Collisions Floor", root),
        collisions_wall=get_or_create_collection("VEX - Collisions Wall", root),
        collisions_reset=get_or_create_collection("VEX - Collisions Reset", root),
        collisions_other=get_or_create_collection("VEX - Collisions Other", root),
        track_mesh=get_or_create_collection("VEX - Track Mesh", root),
        track_guess=get_or_create_collection("VEX - Track Surface (guessed)", root),
        landscape_guess=get_or_create_collection("VEX - Landscape (guessed)", root),
    )


# ============================================================================
# IMPORT
# ============================================================================

def build_mesh_object(name, verts_xyz, faces, collection):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts_xyz, [], faces)
    mesh.validate()
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)
    return obj, mesh


def set_vex_metadata(obj, offsets, scales=None):
    """Stores per-vertex offsets (and optionally scales) as a JSON string in
    an object-level custom property, matched to mesh.vertices purely by
    index order. This is deliberately NOT stored as a mesh custom attribute:
    Blender's generic per-vertex attributes are not reliably preserved
    across Edit Mode sessions in all versions, which caused exports to
    silently lose this data. Object-level custom properties are immune to
    that, at the cost of requiring vertex order/count to stay stable
    (already a hard requirement of this workflow: don't add/remove/reorder
    vertices)."""
    obj['vex_offsets'] = json.dumps(offsets)
    if scales is not None:
        obj['vex_scales'] = json.dumps(scales)


def _build_collision_objects(data, nodes, collections, report):
    """Builds one object per collision category, each in its own dedicated
    collection. Returns (n_vertices, skipped_nodes)."""
    coll_nodes = [n for n in nodes if 'collision' in n['name'].lower()]
    skipped = []
    n_verts_total = 0

    for category in ('collisions_floor', 'collisions_wall', 'collisions_reset', 'collisions_other'):
        cat_nodes = [n for n in coll_nodes if category_of(n['name']) == category]
        if not cat_nodes:
            continue
        verts_xyz = []
        faces = []
        offsets = []
        for n in cat_nodes:
            try:
                parsed = parse_collision_node(data, n['poff'], n['plen'])
            except NotACollisionNode as e:
                skipped.append((n['name'], str(e)))
                continue
            for block_verts, tris in iter_collision_vertex_blocks(parsed):
                base = len(verts_xyz)
                for v in block_verts:
                    verts_xyz.append(game_to_blender(v['x'], v['y'], v['z']))
                    offsets.append(v['file_offset'])
                for a, b, c in tris:
                    if a < len(block_verts) and b < len(block_verts) and c < len(block_verts):
                        faces.append((base + a, base + b, base + c))
        if not verts_xyz:
            continue
        obj, mesh = build_mesh_object(category, verts_xyz, faces, collections[category])
        set_vex_metadata(obj, offsets)
        obj['vex_fmt'] = 'f32'
        obj['vex_orig_vcount'] = len(verts_xyz)
        n_verts_total += len(verts_xyz)
        report({'INFO'}, f"Created '{category}': {len(verts_xyz)} vertices, {len(faces)} faces.")

    return n_verts_total, skipped


def _build_track_mesh_object(data, nodes, collections, report):
    """Builds track mesh object(s) from mesh nodes (VEXX signature 0x11e).

    Two strategies, tried in order:
    1. Name-based (preferred when available): nodes named 'Track_Shape*' are
       merged into a single 'track_mesh' object in the "Track Mesh"
       collection. This matches files where the track surface was
       deliberately named apart from scenery.
    2. Signature-based fallback: if no 'Track_Shape*' node exists (some
       files only use Maya's default names like 'polySurfaceShape*',
       'pCubeShape*', etc., with no naming convention separating the track
       from buildings/props/etc.), every mesh-type node (signature 0x11e)
       is imported as its OWN object, named after the node, and sorted
       into "Track Surface (guessed)" or "Landscape (guessed)" based on
       whether its name contains "track" - a heuristic, not a certainty.
       You can freely drag objects between the two collections afterwards;
       it has no effect on export.
       Either way, this lets you hide or delete whole objects you don't
       care about safely: export only patches vertices of objects that
       still exist, so removing an unwanted object simply leaves that
       geometry untouched in the output file.
    """
    MESH_SIGNATURE = 0x11e

    def build_from_node_list(node_list, merge_into_one, base_name, target_collection=None):
        node_errors = []
        total_verts = 0
        n_objects_created = 0
        if merge_into_one:
            verts_xyz, faces, offsets, scales = [], [], [], []

        for n in node_list:
            try:
                node = parse_mesh_node(data, n['poff'], n['plen'])
                if node['external']:
                    continue
                if not merge_into_one:
                    verts_xyz, faces, offsets, scales = [], [], [], []
                for chunk in node['chunks']:
                    sec1 = extract_chunk_vertices(data, chunk, section=1)
                    sec2 = extract_chunk_vertices(data, chunk, section=2)
                    sections = [s for s in (sec1, sec2) if s]
                    if not sections:
                        continue
                    triangle_strip = chunk['header']['primitive_type'] == PrimitiveType.TRIANGLE_STRIP
                    compact_verts, tris = pack_mesh_data(sections, triangle_strip=triangle_strip)
                    if not compact_verts:
                        continue
                    base = len(verts_xyz)
                    scale = chunk['scaling'] / 32767.0
                    for v in compact_verts:
                        verts_xyz.append(game_to_blender(v['x'], v['y'], v['z']))
                        offsets.append(v['file_offset'])
                        scales.append(scale)
                    for a, b, c in tris:
                        faces.append((base + a, base + b, base + c))
                if not merge_into_one and verts_xyz:
                    obj_name = n['name'][:63]  # Blender object name length limit
                    dest = collections['track_guess'] if looks_like_track_surface(n['name']) \
                        else collections['landscape_guess']
                    obj, mesh = build_mesh_object(obj_name, verts_xyz, faces, dest)
                    set_vex_metadata(obj, offsets, scales)
                    obj['vex_fmt'] = 'trackshape_i16'
                    obj['vex_orig_vcount'] = len(verts_xyz)
                    total_verts += len(verts_xyz)
                    n_objects_created += 1
            except Exception as e:
                node_errors.append(f"{n['name']}: {e}")

        if merge_into_one and verts_xyz:
            obj, mesh = build_mesh_object(base_name, verts_xyz, faces, target_collection)
            set_vex_metadata(obj, offsets, scales)
            obj['vex_fmt'] = 'trackshape_i16'
            obj['vex_orig_vcount'] = len(verts_xyz)
            total_verts += len(verts_xyz)
            n_objects_created += 1
            report({'INFO'}, f"Created '{base_name}': {len(verts_xyz)} vertices, {len(faces)} faces.")

        return total_verts, node_errors, n_objects_created

    ts_nodes = [n for n in nodes if n['name'].startswith('Track_Shape')]
    if ts_nodes:
        n_verts, node_errors, _ = build_from_node_list(
            ts_nodes, merge_into_one=True, base_name='track_mesh',
            target_collection=collections['track_mesh'])
        if node_errors:
            report({'WARNING'}, f"{len(node_errors)} Track_Shape node(s) failed to parse and "
                                 f"were skipped, e.g.: {node_errors[0]}")
        if n_verts > 0:
            return n_verts

    # Fallback: no 'Track_Shape*' naming convention in this file - import every
    # mesh-type node individually so unwanted scenery can be hidden/deleted safely.
    mesh_nodes = [n for n in nodes if n['signature'] == MESH_SIGNATURE]
    if not mesh_nodes:
        report({'ERROR'}, "No mesh-type nodes (signature 0x11e) found in this file at all.")
        return 0

    report({'WARNING'}, f"No 'Track_Shape*' naming convention found in this file - falling "
                         f"back to importing all {len(mesh_nodes)} mesh node(s) as separate "
                         f"objects, sorted into 'Track Surface (guessed)' / 'Landscape "
                         f"(guessed)' by a name heuristic. Double-check the split and drag "
                         f"objects between the two collections if needed.")
    n_verts, node_errors, n_objects = build_from_node_list(mesh_nodes, merge_into_one=False, base_name=None)
    if node_errors:
        report({'WARNING'}, f"{len(node_errors)} mesh node(s) failed to parse and were "
                             f"skipped, e.g.: {node_errors[0]}")
    if n_verts == 0:
        report({'ERROR'}, "No track mesh vertices could be extracted even with the "
                           "signature-based fallback.")
    else:
        report({'INFO'}, f"Created {n_objects} mesh object(s), {n_verts} vertices total.")
    return n_verts


class VEX_OT_import(bpy.types.Operator, ImportHelper):
    bl_idname = "vex.import_track"
    bl_label = "Import VEX Track"
    bl_options = {'REGISTER', 'UNDO'}

    filename_ext = ".vex"
    filter_glob: StringProperty(default="*.vex", options={'HIDDEN'})

    def execute(self, context):
        filepath = self.filepath
        try:
            with open(filepath, 'rb') as f:
                data = f.read()
        except OSError as e:
            self.report({'ERROR'}, f"Could not read file: {e}")
            return {'CANCELLED'}

        try:
            root = vexx_parse_tree(data)
        except Exception as e:
            self.report({'ERROR'}, f"Could not parse VEXX node tree: {e}")
            return {'CANCELLED'}

        nodes = vexx_flatten(root)
        collections = setup_vex_collections(context)
        n_verts_total = 0

        try:
            n_coll, skipped = _build_collision_objects(data, nodes, collections, self.report)
            n_verts_total += n_coll
            if skipped:
                self.report({'WARNING'}, f"Skipped non-standard collision node(s): "
                                          f"{[s[0] for s in skipped]}")
        except Exception:
            self.report({'ERROR'}, "Collision import failed:\n" + traceback.format_exc())

        try:
            n_track = _build_track_mesh_object(data, nodes, collections, self.report)
            n_verts_total += n_track
        except Exception:
            self.report({'ERROR'}, "Track mesh import failed:\n" + traceback.format_exc())

        if n_verts_total == 0:
            self.report({'ERROR'}, "Nothing was imported - see errors above.")
            return {'CANCELLED'}

        context.scene['vex_source_path'] = filepath
        context.scene['vex_source_size'] = len(data)

        self.report({'INFO'}, f"Import finished: {n_verts_total} vertices total.")
        return {'FINISHED'}


# ============================================================================
# EXPORT
# ============================================================================

class VEX_OT_export(bpy.types.Operator, ExportHelper):
    bl_idname = "vex.export_track"
    bl_label = "Export VEX Track"
    bl_options = {'REGISTER'}

    filename_ext = ".vex"
    filter_glob: StringProperty(default="*.vex", options={'HIDDEN'})

    def execute(self, context):
        # If we're still in Edit Mode, the mesh data block does not yet reflect
        # the latest vertex positions (Blender keeps edits in a separate BMesh
        # until you leave Edit Mode). Force a mode switch so reads below always
        # see up-to-date coordinates - this was the cause of exports silently
        # containing no changes.
        if context.mode == 'EDIT_MESH':
            bpy.ops.object.mode_set(mode='OBJECT')
            self.report({'INFO'}, "Left Edit Mode before exporting so current vertex "
                                   "positions are used.")

        src_path = context.scene.get('vex_source_path')
        if not src_path:
            self.report({'ERROR'}, "No known source .vex file. Import a track with this "
                                    "add-on first (export patches the original file, it does "
                                    "not build one from scratch).")
            return {'CANCELLED'}

        try:
            with open(src_path, 'rb') as f:
                data = bytearray(f.read())
        except OSError as e:
            self.report({'ERROR'}, f"Could not reopen the source file '{src_path}': {e}")
            return {'CANCELLED'}

        orig_size = context.scene.get('vex_source_size')
        if orig_size is not None and len(data) != orig_size:
            self.report({'WARNING'}, "The source file's size has changed since the last "
                                      "import - proceeding anyway, but double-check you picked "
                                      "the right file.")

        n_patched = 0
        n_objects = 0
        errors = []

        for obj in bpy.data.objects:
            fmt = obj.get('vex_fmt')
            if fmt is None or obj.type != 'MESH':
                continue

            try:
                mesh = obj.data
                n = len(mesh.vertices)
                orig_n = obj.get('vex_orig_vcount')
                if orig_n is not None and n != orig_n:
                    errors.append(f"'{obj.name}': {n} vertices now vs {orig_n} originally - "
                                   f"did you add/remove vertices? Object skipped.")
                    continue

                offsets_json = obj.get('vex_offsets')
                if offsets_json is None:
                    errors.append(f"'{obj.name}': missing vex_offsets data - object skipped.")
                    continue
                offsets = json.loads(offsets_json)
                if len(offsets) != n:
                    errors.append(f"'{obj.name}': vex_offsets has {len(offsets)} entries but "
                                   f"the mesh has {n} vertices - object skipped. This usually "
                                   f"means the object was duplicated, joined, or otherwise "
                                   f"edited in a way that broke its per-vertex VEX data; if "
                                   f"this object wasn't one you meant to modify, this is "
                                   f"harmless (its geometry is simply left untouched in the "
                                   f"output).")
                    continue

                scales = None
                if fmt == 'trackshape_i16':
                    scales_json = obj.get('vex_scales')
                    if scales_json is None:
                        errors.append(f"'{obj.name}': missing vex_scales data - object skipped.")
                        continue
                    scales = json.loads(scales_json)
                    if len(scales) != n:
                        errors.append(f"'{obj.name}': vex_scales has {len(scales)} entries but "
                                       f"the mesh has {n} vertices - object skipped.")
                        continue

                coords = [0.0] * (n * 3)
                mesh.vertices.foreach_get('co', coords)
                world_matrix = obj.matrix_world

                n_objects += 1
                for i in range(n):
                    local = Vector((coords[i * 3], coords[i * 3 + 1], coords[i * 3 + 2]))
                    world = world_matrix @ local
                    bx, by, bz = world.x, world.y, world.z
                    gx, gy, gz = blender_to_game(bx, by, bz)
                    off = offsets[i]

                    if fmt == 'f32':
                        struct.pack_into('<3f', data, off, gx, gy, gz)
                        n_patched += 1
                    elif fmt == 'trackshape_i16':
                        scale = scales[i]
                        if scale == 0:
                            errors.append(f"'{obj.name}' vertex {i}: scale=0, skipped for safety.")
                            continue
                        rx = max(-32768, min(32767, round(gx / scale)))
                        ry = max(-32768, min(32767, round(gy / scale)))
                        rz = max(-32768, min(32767, round(gz / scale)))
                        struct.pack_into('<3h', data, off, rx, ry, rz)
                        n_patched += 1
            except Exception:
                errors.append(f"'{obj.name}': unexpected error, object skipped:\n"
                               + traceback.format_exc())
                continue

        if n_objects == 0:
            self.report({'ERROR'}, "No object with VEX data found in the scene (no object has "
                                    "the 'vex_fmt' custom property). Did you import with this "
                                    "add-on?")
            return {'CANCELLED'}

        try:
            with open(self.filepath, 'wb') as f:
                f.write(data)
        except OSError as e:
            self.report({'ERROR'}, f"Could not write output file: {e}")
            return {'CANCELLED'}

        msg = f"Wrote {n_patched} vertices from {n_objects} object(s) to {self.filepath}."
        if errors:
            msg += f" {len(errors)} issue(s) - see warnings below."
            for e in errors[:20]:
                self.report({'WARNING'}, e)
        self.report({'INFO'}, msg)
        return {'FINISHED'}


# ============================================================================
# UI
# ============================================================================

class VEX_PT_panel(bpy.types.Panel):
    bl_label = "VEX Tools"
    bl_idname = "VEX_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'VEX Tools'

    def draw(self, context):
        layout = self.layout
        layout.operator("vex.import_track", icon='IMPORT')
        layout.operator("vex.export_track", icon='EXPORT')

        src = context.scene.get('vex_source_path')
        box = layout.box()
        if src:
            box.label(text="Source:", icon='FILE')
            box.label(text=src)
        else:
            box.label(text="No file imported in this session yet.")

        layout.separator()
        layout.label(text="Objects are organized under the")
        layout.label(text="'VEX Track' collection, with one")
        layout.label(text="sub-collection per category.")

        layout.separator()
        layout.label(text="Reminders:")
        col = layout.column(align=True)
        col.scale_y = 0.8
        col.label(text="- Only move existing vertices")
        col.label(text="- No Extrude / Merge / Delete / Subdivide")
        col.label(text="- Whole objects can be safely hidden/deleted")


# ============================================================================
# REGISTRATION
# ============================================================================

classes = (VEX_OT_import, VEX_OT_export, VEX_PT_panel)


def register():
    for c in classes:
        bpy.utils.register_class(c)


def unregister():
    for c in classes:
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()
