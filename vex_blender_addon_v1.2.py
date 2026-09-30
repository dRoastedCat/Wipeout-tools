bl_info = {
    "name": "WipEout VEX Track Tools",
    "author": "Claude + droastedcat + community reverse engineering (PierreBelmondo/vscode-wipeout)",
    "version": (1, 2, 0),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > VEX Tools",
    "description": "Import/export WipEout Pure/Pulse track collision and mesh geometry (.vex)",
    "category": "Import-Export",
}

import bpy
import struct
import json
import math
import traceback
from mathutils import Vector, Matrix
from bpy.props import StringProperty, BoolProperty
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


def _parse_material(data, off):
    """VexxNodeMeshMaterial - 20 bytes. Only textureId is needed here."""
    texture_id = struct.unpack_from('<I', data, off + 4)[0]
    return dict(texture_id=texture_id, size=20)


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
        return dict(external=True, chunks=[], materials=[])

    n_materials = header['mesh_count']
    materials = []
    moff = poff + header['size']
    for i in range(n_materials):
        m = _parse_material(data, moff)
        materials.append(m)
        moff += m['size']

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

    return dict(external=False, chunks=chunks, materials=materials)


def extract_chunk_uvs(data, chunk, section=1):
    """Estrae le UV (u,v) per ogni vertice dello stesso chunk/sezione usate
    da extract_chunk_vertices, nello stesso ordine (incluse le sentinelle
    None per i vertici di restart, cosi' l'allineamento con le posizioni
    resta corretto prima della compattazione in pack_mesh_data)."""
    si = chunk['stride_info']
    ts = si['texture']['size']
    to = si['texture']['offset']
    ss = chunk['stride_size']
    if section == 1:
        n = chunk['header']['stride_count1']
        base_off = chunk['strides_off']
    else:
        n = chunk['header']['stride_count2']
        base_off = chunk['strides2_off']
    if ts == 0:
        return [None] * n
    uvs = []
    for i in range(n):
        rec_off = base_off + i * ss + to
        if ts == 4:
            u, v = struct.unpack_from('<2f', data, rec_off)
        elif ts == 2:
            ru, rv = struct.unpack_from('<2h', data, rec_off)
            u, v = ru / 32767.0, rv / 32767.0
        elif ts == 1:
            ru, rv = struct.unpack_from('<2b', data, rec_off)
            u, v = ru / 127.0, rv / 127.0
        else:
            u, v = 0.0, 0.0
        uvs.append((u, v))
    return uvs


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


def merge_uv_into_vertices(verts, uvs):
    """Aggiunge le chiavi 'u'/'v' ai dizionari vertice (lasciando le
    sentinelle None invariate), cosi' le UV attraversano pack_mesh_data
    insieme alle posizioni senza bisogno di modificarlo."""
    out = []
    for v, uv in zip(verts, uvs):
        if v is None:
            out.append(None)
            continue
        v = dict(v)
        v['u'], v['v'] = uv if uv is not None else (0.0, 0.0)
        out.append(v)
    return out


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
# Texture nodes (core/formats/vexx/v4/texture.ts) and the "part 2" texture
# data blob layout (core/formats/vexx/index.ts, Vexx.load).
# ============================================================================

TEXTURE_SIGNATURE = 0x373


def parse_texture_node(data, poff):
    width = struct.unpack_from('<H', data, poff + 0)[0]
    height = struct.unpack_from('<H', data, poff + 2)[0]
    bpp = data[poff + 4]
    mipmaps = data[poff + 5]
    fmt = data[poff + 6]
    tex_id = data[poff + 7]
    cmap_size = struct.unpack_from('<I', data, poff + 8)[0]
    data_size = struct.unpack_from('<I', data, poff + 12)[0]
    name_bytes = data[poff + 56:]
    name = name_bytes.split(b'\0', 1)[0].decode(errors='replace')
    return dict(width=width, height=height, bpp=bpp, mipmaps=mipmaps, format=fmt,
                id=tex_id, cmap_size=cmap_size, data_size=data_size, name=name)


def assign_texture_ranges(tex_metas, textures_begin):
    """Assigns cmap/data byte ranges sequentially in traversal order,
    exactly as Vexx.load does in index.ts: for each texture node (in the
    same order they're encountered walking the node tree), cmapSize bytes
    are taken for the palette followed immediately by dataSize bytes for
    the pixel indices, back to back with no padding."""
    offset = textures_begin
    for meta in tex_metas:
        meta['cmap_begin'] = offset
        offset += meta['cmap_size']
        meta['data_begin'] = offset
        offset += meta['data_size']
    return tex_metas


def decode_texture_rgba(data, meta):
    """Decodes the first (largest) mipmap level into a flat RGBA byte
    array, porting VexxNodeTexture.loadTexture/loadMipmap from texture.ts
    (palette lookup + PSP texture-memory unswizzle)."""
    swizzle = bool(meta['format'] & 1)
    cmap_begin = meta['cmap_begin']
    cmap_size = meta['cmap_size']
    data_begin = meta['data_begin']

    block_size = 32 if cmap_size == 64 else 16
    bpp = 4 if meta['bpp'] == 4 else 8
    width = meta['width']
    height = meta['height']

    size = width * height
    rgba = bytearray(size * 4)
    block_real = min(width, block_size)
    blocks = (width * height) // block_real if block_real else 0

    for i in range(blocks):
        block_offset = (i * block_size * bpp) // 8
        idx_off = data_begin + block_offset
        n_idx_bytes = (block_real * bpp) // 8
        idx_bytes = data[idx_off: idx_off + n_idx_bytes]
        for j in range(block_real):
            if bpp == 4:
                b = idx_bytes[j >> 1]
                index = (b & 0b1111) if j % 2 == 0 else (b >> 4)
            else:
                index = idx_bytes[j]
            pixel = j + i * block_real
            cbase = cmap_begin + index * 4
            rgba[pixel * 4:pixel * 4 + 4] = data[cbase:cbase + 4]

    if swizzle and width > block_real:
        ch = 8
        cw = 32 if cmap_size == 64 else 16
        cs = ch * cw
        tmp = bytearray(size * 4)
        n_chunks = size // cs if cs else 0
        cols = width // cw if cw else 1
        for ci in range(n_chunks):
            chunk = rgba[cs * 4 * ci: cs * 4 * (ci + 1)]
            for l in range(ch):
                k = (ci % cols) * cw + (ci // cols) * ch * width + l * width
                for c in range(cw):
                    i_idx = c + k
                    j_idx = c + l * cw
                    tmp[i_idx * 4:i_idx * 4 + 4] = chunk[j_idx * 4:j_idx * 4 + 4]
        return bytes(tmp), width, height

    return bytes(rgba), width, height


def collect_texture_metas(data, all_nodes, textures_begin):
    """Finds every texture node (signature 0x373) in tree order and assigns
    its cmap/data byte ranges. Returns a dict keyed by the texture's own
    'id' byte field (0-255, NOT the tree-traversal index) - this is what
    VexxNodeMeshMaterial.textureId is matched against."""
    tex_nodes = [n for n in all_nodes if n['signature'] == TEXTURE_SIGNATURE]
    metas = [parse_texture_node(data, n['poff']) for n in tex_nodes]
    metas = assign_texture_ranges(metas, textures_begin)
    by_id = {}
    for m in metas:
        by_id[m['id']] = m
    return by_id


def get_transform_matrix(data, node):
    """Reads a 4x4 row-major world transform matrix from a node's 64-byte
    payload, if it has one. Some VEXX nodes are lightweight 'instance'
    wrappers whose entire payload is just a transform for a single child
    (e.g. pad instances, and some collision_wall_freestyle variants) - the
    child's own vertices are defined in local/template space and only
    make sense once this transform is applied. Returns None if the
    payload isn't exactly 64 bytes (16 float32) or doesn't look like a
    valid transform (homogeneous w component isn't ~1.0)."""
    if node['plen'] != 64:
        return None
    vals = struct.unpack_from('<16f', data, node['poff'])
    if abs(vals[15] - 1.0) > 0.01:
        return None
    return vals


def apply_matrix(x, y, z, m):
    """Row-vector * row-major 4x4 matrix: world = [x,y,z,1] @ m."""
    wx = x * m[0] + y * m[4] + z * m[8] + m[12]
    wy = x * m[1] + y * m[5] + z * m[9] + m[13]
    wz = x * m[2] + y * m[6] + z * m[10] + m[14]
    return wx, wy, wz


# Axis-change matrix (column-vector convention: blender_col = AXIS_A @ game_col),
# matching game_to_blender's (x,y,z) -> (x,-z,y). Orthogonal, so its inverse is
# its own transpose.
_AXIS_A = Matrix((
    (1, 0, 0, 0),
    (0, 0, -1, 0),
    (0, 1, 0, 0),
    (0, 0, 0, 1),
))


def wrapper_matrix_to_blender(m):
    """Converts a game-space instance-wrapper matrix (row-major, row-vector
    convention, as returned by get_transform_matrix) into a Blender
    mathutils.Matrix directly usable as obj.matrix_world - i.e. exposing
    the wrapper's translation/rotation as the OBJECT's own transform,
    rather than baking it into vertex coordinates. This is what lets
    moving/rotating the object in Object Mode edit the actual thing the
    game reads for gameplay purposes (e.g. a pad's trigger position),
    instead of only changing how the mesh looks."""
    m0_colvec = Matrix((
        (m[0], m[4], m[8], m[12]),
        (m[1], m[5], m[9], m[13]),
        (m[2], m[6], m[10], m[14]),
        (m[3], m[7], m[11], m[15]),
    ))
    return _AXIS_A @ m0_colvec @ _AXIS_A.transposed()


def blender_matrix_to_wrapper(m_blender):
    """Inverse of wrapper_matrix_to_blender: converts a Blender
    mathutils.Matrix (e.g. obj.matrix_world) back into the flat 16-float
    row-major, row-vector-convention array the file actually stores."""
    m0_colvec = _AXIS_A.transposed() @ m_blender @ _AXIS_A
    flat = [0.0] * 16
    for row in range(4):
        for col in range(4):
            flat[row * 4 + col] = m0_colvec[col][row]
    return flat


def apply_inverse_matrix(x, y, z, m):
    """Inverse of apply_matrix, assuming m's upper-left 3x3 is a pure
    rotation (orthonormal - true for every VEXX instance-wrapper transform
    observed so far): local = (world - T) @ R^T."""
    dx, dy, dz = x - m[12], y - m[13], z - m[14]
    lx = dx * m[0] + dy * m[1] + dz * m[2]
    ly = dx * m[4] + dy * m[5] + dz * m[6]
    lz = dx * m[8] + dy * m[9] + dz * m[10]
    return lx, ly, lz


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
        pads_speedup=get_or_create_collection("VEX - Pads (Speedup)", root),
        pads_weapon=get_or_create_collection("VEX - Pads (Weapon)", root),
        wo_track_lanes=get_or_create_collection("VEX - WO Track Lanes", root),
    )


# ---- WO_TRACK lane lines (racing line visualization) ----

WO_TRACK_SIGNATURE = 0x36D
_WO_TRACK_MAGIC = 0x574F7464  # 'WOtd'
_LANE_COLORS = [(1.0, 0.2, 0.2, 1.0), (0.2, 1.0, 0.3, 1.0),
                (0.3, 0.4, 1.0, 1.0), (1.0, 0.8, 0.1, 1.0),
                (0.9, 0.3, 1.0, 1.0), (0.2, 1.0, 1.0, 1.0),
                (1.0, 0.5, 0.0, 1.0), (0.6, 1.0, 0.6, 1.0),
                (0.5, 0.5, 1.0, 1.0)]


def parse_wotrack_points(payload):
    """Decodes the WO_TRACK payload and returns (lane_point_counts, all_points)
    where all_points is a flat list of (x, y, z) world-space positions in
    traversal order (lane[0] points followed by lane[1] points, etc.)."""
    if struct.unpack_from('<I', payload, 0)[0] != _WO_TRACK_MAGIC:
        return None, None
    laneCount = struct.unpack_from('<I', payload, 8)[0]
    laneGraphRowCount = struct.unpack_from('<I', payload, 12)[0]
    lanes = []
    for i in range(laneCount):
        off = 64 + i * 32
        pc = struct.unpack_from('<I', payload, off)[0]
        lanes.append(pc)
    graph_off = 64 + laneCount * 32
    points_off = graph_off + laneGraphRowCount * 16
    total = sum(lanes)
    points = []
    for i in range(total):
        off = points_off + i * 112
        x, y, z = struct.unpack_from('<3f', payload, off)
        points.append((x, y, z))
    return lanes, points


def build_lane_line_objects(data, nodes, collection):
    """Creates one EDITABLE mesh object per lane, colored differently, from the
    WO_TRACK racing line points.

    Unlike a curve, this is a real mesh whose vertices map 1:1 (by index) to the
    position bytes (+0x00) of the WO_TRACK points. Each vertex carries a
    'wotrack_off' custom property = the file byte offset of that point's position,
    so moving a vertex in Edit Mode and exporting writes the new position back
    into the .vex file (fixing spots where the AI hugs the wall).

    Returns the list of created objects."""
    wo = [n for n in nodes if n['signature'] == WO_TRACK_SIGNATURE]
    if not wo:
        return []
    node = wo[0]
    payload = data[node['poff']: node['poff'] + node['plen']]
    lanes, points = parse_wotrack_points(payload)
    if lanes is None:
        return []

    created = []
    start = 0
    for li, cnt in enumerate(lanes):
        seg = points[start:start + cnt]
        start += cnt
        if len(seg) < 2:
            continue

        # Build a mesh from the lane points (open polyline, not closed).
        coords = [game_to_blender(x, y, z) for (x, y, z) in seg]
        mesh = bpy.data.meshes.new(f"WO_Track_Lane_{li}")
        mesh.from_pydata(coords, [], [])
        mesh.validate()
        mesh.update()

        obj = bpy.data.objects.new(f"WO_Track_Lane_{li}", mesh)
        collection.objects.link(obj)

        # Color per lane
        color = _LANE_COLORS[li % len(_LANE_COLORS)]
        mesh.materials.append(make_lane_material(f"lane_{li}", color))

        # Record, for each vertex, the file byte offset of its position (+0x00)
        # in the WO_TRACK payload. Vertex index i corresponds to point (start+i).
        offsets = []
        laneCount = struct.unpack_from('<I', payload, 8)[0]
        laneGraphRowCount = struct.unpack_from('<I', payload, 12)[0]
        graph_off = 64 + laneCount * 32
        points_off = graph_off + laneGraphRowCount * 16
        for i in range(len(seg)):
            point_off = node['poff'] + points_off + (start + i) * 112
            offsets.append(point_off)

        obj['wotrack_offsets'] = json.dumps(offsets)
        obj['wotrack_orig_vcount'] = len(seg)
        created.append(obj)
    return created


def make_lane_material(name, color):
    """Creates (or returns cached) an emissive-ish material for a lane line."""
    mat = bpy.data.materials.get(name)
    if mat is None:
        mat = bpy.data.materials.new(name)
        mat.use_nodes = True
        bsdf = next((n for n in mat.node_tree.nodes if n.type == 'BSDF_PRINCIPLED'), None)
        if bsdf is not None:
            bsdf.inputs['Base Color'].default_value = color
            if 'Emission Color' in bsdf.inputs:
                bsdf.inputs['Emission Color'].default_value = color
                bsdf.inputs['Emission Strength'].default_value = 1.0
    return mat


# ---- WO_TRACK AI reverse (path Forward <-> Reverse) ----

_LANE_GRAPH_NONE = 0x7FFFFFFF


def _cross(a, b):
    return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])


def _normalize(v):
    n = math.sqrt(v[0]**2 + v[1]**2 + v[2]**2)
    if n > 0:
        return (v[0]/n, v[1]/n, v[2]/n)
    return (0.0, 0.0, 1.0)


def reverse_wotrack_payload(payload):
    """Inverts the AI racing line in a WO_TRACK payload (in place).
    Strategy (verified to work on Modesto Heights and generalize to N lanes):
      - Reverse the point order INSIDE each lane, WITHOUT swapping the lane order.
      - Keep the lane descriptors (pointCount/scale/next/prev) and lane graph
        unchanged, so the AI passes correctly between lanes.
      - Negate forward, keep down, recompute right = forward x down (orthonormal).
      - Swap leftMetric/rightMetric (borders invert).
      - Rebuild param as a monotone arc-length (excluding crossover gaps).
    Returns True if the payload was modified."""
    if struct.unpack_from('<I', payload, 0)[0] != _WO_TRACK_MAGIC:
        return False

    laneCount = struct.unpack_from('<I', payload, 8)[0]
    laneGraphRowCount = struct.unpack_from('<I', payload, 12)[0]

    # Read lane point counts
    lanes = []
    for i in range(laneCount):
        off = 64 + i * 32
        pc = struct.unpack_from('<I', payload, off)[0]
        lanes.append(pc)

    graph_off = 64 + laneCount * 32
    points_off = graph_off + laneGraphRowCount * 16
    total = sum(lanes)

    # Read all points into a list of mutable dicts
    POINT_STRIDE = 112
    pts = []
    for i in range(total):
        off = points_off + i * POINT_STRIDE
        pos = list(struct.unpack_from('<4f', payload, off))
        right = list(struct.unpack_from('<4f', payload, off + 16))
        down = list(struct.unpack_from('<4f', payload, off + 32))
        fwd = list(struct.unpack_from('<4f', payload, off + 48))
        param = struct.unpack_from('<f', payload, off + 64)[0]
        left = struct.unpack_from('<f', payload, off + 68)[0]
        rightm = struct.unpack_from('<f', payload, off + 72)[0]
        u0 = struct.unpack_from('<f', payload, off + 76)[0]
        u1 = struct.unpack_from('<f', payload, off + 80)[0]
        tail = bytes(payload[off + 84: off + 112])
        pts.append(dict(pos=pos, right=right, down=down, fwd=fwd, param=param,
                        left=left, rightm=rightm, u0=u0, u1=u1, tail=tail))

    # Split into lanes, reverse each lane in place (do NOT swap lane order)
    new_points = []
    start = 0
    for cnt in lanes:
        seg = pts[start:start + cnt]
        new_points.extend(reversed(seg))
        start += cnt

    n = len(new_points)

    # Reverse vectors keeping orthonormal basis
    for p in new_points:
        fwd_old = p['fwd'][:3]
        down_old = p['down'][:3]
        fwd_new = (-fwd_old[0], -fwd_old[1], -fwd_old[2])
        down_new = down_old
        right_new = _normalize(_cross(fwd_new, down_new))
        p['fwd'] = [fwd_new[0], fwd_new[1], fwd_new[2], 0.0]
        p['down'] = [down_new[0], down_new[1], down_new[2], 0.0]
        p['right'] = [right_new[0], right_new[1], right_new[2], 0.0]
        p['left'], p['rightm'] = p['rightm'], p['left']

    # Rebuild param as monotone arc-length, excluding crossover gaps
    seg_dists = []
    for i in range(1, n):
        a = new_points[i-1]['pos'][:3]
        b = new_points[i]['pos'][:3]
        seg_dists.append(math.dist(a, b))
    a = new_points[-1]['pos'][:3]
    b = new_points[0]['pos'][:3]
    seg_dists.append(math.dist(a, b))
    sorted_d = sorted(seg_dists)
    median = sorted_d[len(sorted_d)//2] if sorted_d else 1.0
    threshold = median * 10.0
    cum = [0.0]
    total_dist = 0.0
    for i in range(1, n):
        d = seg_dists[i-1]
        if d > threshold:
            d = median
        total_dist += d
        cum.append(total_dist)
    d_close = seg_dists[-1]
    if d_close > threshold:
        d_close = median
    total_dist += d_close
    if total_dist > 0:
        for i, p in enumerate(new_points):
            p['param'] = cum[i] / total_dist
    else:
        for p in new_points:
            p['param'] = 0.0

    # Write back the points (lane descriptors / graph left unchanged)
    for i, p in enumerate(new_points):
        off = points_off + i * POINT_STRIDE
        struct.pack_into('<4f', payload, off, *p['pos'])
        struct.pack_into('<4f', payload, off + 16, *p['right'])
        struct.pack_into('<4f', payload, off + 32, *p['down'])
        struct.pack_into('<4f', payload, off + 48, *p['fwd'])
        struct.pack_into('<f', payload, off + 64, p['param'])
        struct.pack_into('<f', payload, off + 68, p['left'])
        struct.pack_into('<f', payload, off + 72, p['rightm'])
        struct.pack_into('<f', payload, off + 76, p['u0'])
        struct.pack_into('<f', payload, off + 80, p['u1'])
        payload[off + 84: off + 112] = p['tail']

    return True


def get_source_payload(data, nodes):
    """Returns (payload_bytearray, payload_len) for the WO_TRACK node, or None."""
    wo = [n for n in nodes if n['signature'] == WO_TRACK_SIGNATURE]
    if not wo:
        return None
    node = wo[0]
    payload = bytearray(data[node['poff']: node['poff'] + node['plen']])
    return payload, node['poff'], node['plen']


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


def _is_empty_transform_marker(n):
    """True for nodes that are a plain, childless VexxNodeTransform
    (signature 0x6d) with a 64-byte payload (just a 4x4 matrix, no
    geometry) - regardless of what the artist named it. Some tracks
    contain dozens of these under collision-suggestive names like
    'collision_wall_freestyleN'; they appear to be leftover Maya
    authoring locators/guide transforms that were never wired up to real
    collision geometry, since the actual VEXX engine dispatches behavior
    by node TYPE (this signature), not by name - it would treat these
    identically to any other empty Transform node. Skipping them loses
    no in-game collision data."""
    return n['signature'] == 0x6d and n['nch'] == 0 and n['plen'] == 64


def _build_collision_objects(data, nodes, collections, report):
    """Builds one object per collision category, each in its own dedicated
    collection. Returns (n_vertices, skipped_nodes)."""
    coll_nodes = [n for n in nodes if 'collision' in n['name'].lower()]
    skipped = []
    n_empty_markers = 0
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
                if _is_empty_transform_marker(n):
                    n_empty_markers += 1
                else:
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

    if n_empty_markers:
        report({'INFO'}, f"{n_empty_markers} collision-named node(s) were empty transform "
                          f"markers with no attached geometry (harmless - typically leftover "
                          f"authoring locators; no in-game collision data was skipped).")

    return n_verts_total, skipped


def get_or_create_material(texture_id, tex_metas_by_id, data, material_cache):
    """Returns a cached Blender material for a given VEXX texture id,
    decoding the texture and building the material/image on first use.
    Returns None if the id has no matching texture (also cached, so a
    missing texture is only looked up once)."""
    if texture_id is None or tex_metas_by_id is None:
        return None
    if texture_id in material_cache:
        return material_cache[texture_id]

    meta = tex_metas_by_id.get(texture_id)
    if meta is None:
        material_cache[texture_id] = None
        return None

    try:
        rgba, w, h = decode_texture_rgba(data, meta)
    except Exception:
        material_cache[texture_id] = None
        return None

    raw_name = meta['name'].replace('\\', '/').split('/')[-1] or f"tex_{texture_id}"
    image = bpy.data.images.new(raw_name[:63], width=w, height=h, alpha=True)
    floats = [c / 255.0 for c in rgba]
    row_bytes = w * 4
    # Blender's pixel buffer is bottom-up; our decoded data is top-down.
    flipped = []
    for row in range(h - 1, -1, -1):
        flipped.extend(floats[row * row_bytes:(row + 1) * row_bytes])
    image.pixels = flipped
    image.pack()

    mat = bpy.data.materials.new((raw_name[:50] + f"_{texture_id}")[:63])
    mat.use_nodes = True
    node_tree = mat.node_tree
    bsdf = next((n for n in node_tree.nodes if n.type == 'BSDF_PRINCIPLED'), None)
    tex_node = node_tree.nodes.new('ShaderNodeTexImage')
    tex_node.image = image
    if bsdf is not None:
        node_tree.links.new(tex_node.outputs['Color'], bsdf.inputs['Base Color'])
        if 'Alpha' in bsdf.inputs:
            node_tree.links.new(tex_node.outputs['Alpha'], bsdf.inputs['Alpha'])
    mat.blend_method = 'CLIP'

    material_cache[texture_id] = mat
    return mat


def _extract_mesh_nodes_to_objects(data, node_list, merge_into_one, base_name,
                                    target_collection=None, per_node_collection=None,
                                    tex_metas_by_id=None, material_cache=None,
                                    node_transforms=None):
    """Shared helper: parses a list of VEXX mesh-type nodes (anything built on
    VexxNodeMesh - generic track/scenery meshes, speedup pads, weapon pads...)
    and builds either one merged Blender object (merge_into_one=True, placed
    in target_collection) or one object per node (merge_into_one=False,
    placed via per_node_collection(node_name) -> Collection).

    When tex_metas_by_id is provided, also extracts UV coordinates and
    assigns per-chunk materials (with decoded textures) - resolved via each
    chunk's material id -> VexxNodeMeshMaterial.textureId -> matching
    texture node. Pass None to skip texture/UV work entirely (faster, and
    the only option when the file's textures section couldn't be parsed).

    When node_transforms is provided, it's a dict keyed by id(node) mapping
    to a 4x4 row-major matrix (see get_transform_matrix) that is applied to
    that node's vertices before the game->Blender axis conversion. Needed
    for nodes whose own geometry is defined in local/template space (e.g.
    pads), with their world position/orientation held by a separate wrapper
    node instead of baked into the vertices themselves."""
    want_textures = tex_metas_by_id is not None
    node_errors = []
    total_verts = 0
    n_objects_created = 0

    def new_accumulators():
        return dict(verts_xyz=[], faces=[], offsets=[], scales=[], uvs=[],
                    face_mat_idx=[], slot_of_texture={}, materials_in_order=[])

    acc = new_accumulators() if merge_into_one else None

    for n in node_list:
        try:
            node = parse_mesh_node(data, n['poff'], n['plen'])
            if node['external']:
                continue
            if not merge_into_one:
                acc = new_accumulators()

            xform = node_transforms.get(id(n)) if node_transforms else None

            for chunk in node['chunks']:
                sec1 = extract_chunk_vertices(data, chunk, section=1)
                sec2 = extract_chunk_vertices(data, chunk, section=2)
                if want_textures:
                    uv1 = extract_chunk_uvs(data, chunk, section=1)
                    uv2 = extract_chunk_uvs(data, chunk, section=2)
                    sec1 = merge_uv_into_vertices(sec1, uv1)
                    sec2 = merge_uv_into_vertices(sec2, uv2)
                sections = [s for s in (sec1, sec2) if s]
                if not sections:
                    continue
                triangle_strip = chunk['header']['primitive_type'] == PrimitiveType.TRIANGLE_STRIP
                compact_verts, tris = pack_mesh_data(sections, triangle_strip=triangle_strip)
                if not compact_verts:
                    continue

                mat = None
                if want_textures:
                    chunk_id = chunk['header']['id']
                    if chunk_id < len(node['materials']):
                        texture_id = node['materials'][chunk_id]['texture_id']
                        mat = get_or_create_material(texture_id, tex_metas_by_id, data, material_cache)
                slot = None
                if mat is not None:
                    if mat.name not in acc['slot_of_texture']:
                        acc['slot_of_texture'][mat.name] = len(acc['materials_in_order'])
                        acc['materials_in_order'].append(mat)
                    slot = acc['slot_of_texture'][mat.name]

                base = len(acc['verts_xyz'])
                scale = chunk['scaling'] / 32767.0
                for v in compact_verts:
                    acc['verts_xyz'].append(game_to_blender(v['x'], v['y'], v['z']))
                    acc['offsets'].append(v['file_offset'])
                    acc['scales'].append(scale)
                    if want_textures:
                        acc['uvs'].append((v.get('u', 0.0), v.get('v', 0.0)))
                for a, b, c in tris:
                    acc['faces'].append((base + a, base + b, base + c))
                    if want_textures:
                        acc['face_mat_idx'].append(slot if slot is not None else 0)

            if not merge_into_one and acc['verts_xyz']:
                obj_name = n['name'][:63]  # Blender object name length limit
                dest = per_node_collection(n['name'])
                verts_for_mesh = acc['verts_xyz']
                if xform is not None:
                    # Recenter the object's origin on the mesh's own
                    # geometric center (rather than wherever local (0,0,0)
                    # in the raw template happens to sit), so rotating the
                    # object in Blender pivots around the pad itself. The
                    # mesh is shifted by -centroid, and the object's
                    # transform is adjusted by +R@centroid to compensate -
                    # the shift becomes part of what "local space" means
                    # for this object from here on, so export must NOT try
                    # to undo it: it just needs to write these (shifted)
                    # coordinates and this (compensated) transform
                    # consistently, which it does automatically.
                    nv = len(verts_for_mesh)
                    cx = sum(v[0] for v in verts_for_mesh) / nv
                    cy = sum(v[1] for v in verts_for_mesh) / nv
                    cz = sum(v[2] for v in verts_for_mesh) / nv
                    verts_for_mesh = [(vx - cx, vy - cy, vz - cz) for vx, vy, vz in verts_for_mesh]
                obj, mesh = build_mesh_object(obj_name, verts_for_mesh, acc['faces'], dest)
                set_vex_metadata(obj, acc['offsets'], acc['scales'])
                obj['vex_fmt'] = 'trackshape_i16'
                obj['vex_orig_vcount'] = len(acc['verts_xyz'])
                if xform is not None:
                    # Expose the wrapper's world transform as the OBJECT's
                    # own transform (rather than baking it into vertex
                    # coordinates), so moving/rotating the object in Object
                    # Mode edits the actual thing the game reads for
                    # gameplay purposes (e.g. a pad's trigger position) -
                    # not just how the mesh looks. See export for the
                    # inverse conversion written back to the wrapper node.
                    M = wrapper_matrix_to_blender(xform['matrix'])
                    R = M.to_3x3()
                    shift = R @ Vector((cx, cy, cz))
                    M = M.copy()
                    M.translation = M.translation + shift
                    obj.matrix_world = M
                    obj['vex_wrapper_offset'] = xform['wrapper_offset']
                if want_textures and acc['materials_in_order']:
                    apply_uvs_and_materials(mesh, acc['uvs'], acc['materials_in_order'], acc['face_mat_idx'])
                total_verts += len(acc['verts_xyz'])
                n_objects_created += 1
        except Exception as e:
            node_errors.append(f"{n['name']}: {e}")

    if merge_into_one and acc['verts_xyz']:
        obj, mesh = build_mesh_object(base_name, acc['verts_xyz'], acc['faces'], target_collection)
        set_vex_metadata(obj, acc['offsets'], acc['scales'])
        obj['vex_fmt'] = 'trackshape_i16'
        obj['vex_orig_vcount'] = len(acc['verts_xyz'])
        if want_textures and acc['materials_in_order']:
            apply_uvs_and_materials(mesh, acc['uvs'], acc['materials_in_order'], acc['face_mat_idx'])
        total_verts += len(acc['verts_xyz'])
        n_objects_created += 1

    return total_verts, node_errors, n_objects_created


def apply_uvs_and_materials(mesh, uvs, materials_in_order, face_mat_idx):
    """Creates a UV map (per-vertex UV, looked up per-loop) and assigns
    material slots + per-face material index to a freshly built mesh."""
    if uvs:
        uv_layer = mesh.uv_layers.new(name="UVMap")
        for loop in mesh.loops:
            uv = uvs[loop.vertex_index]
            uv_layer.data[loop.index].uv = uv
    for mat in materials_in_order:
        mesh.materials.append(mat)
    if len(materials_in_order) > 1 and face_mat_idx:
        mesh.polygons.foreach_set('material_index', face_mat_idx)


def _build_track_mesh_object(data, nodes, collections, report, tex_metas_by_id=None, material_cache=None):
    """Builds track mesh object(s) from mesh nodes (VEXX signature 0x11e).

    Two strategies, tried in order:
    1. Name-based (preferred when available): nodes named 'Track_Shape*' are
       merged into a single 'track_mesh' object in the "Track Mesh"
       collection. This matches files where the track surface was
       deliberately named apart from scenery.
    2. Signature-based catch-all: every OTHER mesh-type node (signature
       0x11e) not already covered by step 1 - whether because the file
       uses no naming convention at all, or (as turns out to be common)
       only PART of its geometry follows the 'Track_Shape*' convention
       while the rest keeps Maya's default export names ('polySurfaceShape*',
       'pCubeShape*', etc.) - is imported as its OWN object, named after
       the node, and sorted into "Track Surface (guessed)" or "Landscape
       (guessed)" based on whether its name contains "track" - a
       heuristic, not a certainty. You can freely drag objects between the
       two collections afterwards; it has no effect on export.
       This step always runs, even when step 1 found plenty of geometry:
       an earlier version of this add-on skipped it whenever any
       'Track_Shape*' node existed, silently dropping any differently-named
       geometry mixed into the same file - including, on at least one
       real track, a large chunk of visible track surface that used a
       generic name. Fixed.
       Either way, this lets you hide or delete whole objects you don't
       care about safely: export only patches vertices of objects that
       still exist, so removing an unwanted object simply leaves that
       geometry untouched in the output file.
    """
    MESH_SIGNATURE = 0x11e
    total_verts = 0

    ts_nodes = [n for n in nodes if n['name'].startswith('Track_Shape')]
    if ts_nodes:
        n_verts, node_errors, _ = _extract_mesh_nodes_to_objects(
            data, ts_nodes, merge_into_one=True, base_name='track_mesh',
            target_collection=collections['track_mesh'],
            tex_metas_by_id=tex_metas_by_id, material_cache=material_cache)
        if node_errors:
            report({'WARNING'}, f"{len(node_errors)} Track_Shape node(s) failed to parse and "
                                 f"were skipped, e.g.: {node_errors[0]}")
        if n_verts > 0:
            report({'INFO'}, f"Created 'track_mesh': {n_verts} vertices.")
        total_verts += n_verts

    # Always also import every mesh-type node NOT named 'Track_Shape*' - see
    # docstring above for why this must not be skipped just because step 1
    # found something.
    remaining_nodes = [n for n in nodes
                        if n['signature'] == MESH_SIGNATURE and not n['name'].startswith('Track_Shape')]
    if not remaining_nodes:
        if total_verts == 0:
            report({'ERROR'}, "No mesh-type nodes (signature 0x11e) found in this file at all.")
        return total_verts

    per_node = lambda name: collections['track_guess'] if looks_like_track_surface(name) \
        else collections['landscape_guess']
    n_verts, node_errors, n_objects = _extract_mesh_nodes_to_objects(
        data, remaining_nodes, merge_into_one=False, base_name=None, per_node_collection=per_node,
        tex_metas_by_id=tex_metas_by_id, material_cache=material_cache)
    if node_errors:
        report({'WARNING'}, f"{len(node_errors)} mesh node(s) failed to parse and were "
                             f"skipped, e.g.: {node_errors[0]}")
    if n_verts == 0 and total_verts == 0:
        report({'ERROR'}, "No track mesh vertices could be extracted even with the "
                           "signature-based fallback.")
    elif n_verts > 0:
        report({'INFO'}, f"Created {n_objects} additional mesh object(s) not covered by the "
                          f"'Track_Shape*' naming convention, {n_verts} vertices total - sorted "
                          f"into 'Track Surface (guessed)' / 'Landscape (guessed)' by a name "
                          f"heuristic. Double-check the split and drag objects between the two "
                          f"collections if needed.")
    total_verts += n_verts
    return total_verts


def _build_pad_objects(data, nodes, collections, report, tex_metas_by_id=None, material_cache=None):
    """Builds one object per speedup/weapon pad, sorted into separate
    'VEX - Pads (Speedup)' / 'VEX - Pads (Weapon)' collections. The two
    node types (VexxNodeSpeedupPad, signature 0x36f, and VexxNodeWeaponPad,
    signature 0x370) are subclasses of VexxNodeMesh with an identical
    binary layout to generic track mesh nodes, so the same parser is reused
    as-is - only the destination collection differs.

    Each pad's actual geometry node is nested one level inside a lightweight
    'instance' wrapper node whose entire 64-byte payload is a 4x4 world
    transform (see get_transform_matrix) - the geometry itself is defined
    in local/template space (a single small pad shape reused at every
    track position) and only makes sense once that transform is applied.
    This wrapper/child relationship is found by walking each node's
    children rather than by name, since wrapper names vary
    ('Speedup1:speedup_pad_nolight', 'Weapon8:weapon_pad_nolight', etc.)."""
    SPEEDUP_PAD_SIGNATURE = 0x36f
    WEAPON_PAD_SIGNATURE = 0x370

    total_verts = 0
    for signature, label, dest_key in (
        (SPEEDUP_PAD_SIGNATURE, 'speedup pad', 'pads_speedup'),
        (WEAPON_PAD_SIGNATURE, 'weapon pad', 'pads_weapon'),
    ):
        pad_nodes = []
        node_transforms = {}
        for n in nodes:
            for child in n.get('children', []):
                if child['signature'] == signature:
                    pad_nodes.append(child)
                    mat = get_transform_matrix(data, n)
                    if mat is not None:
                        node_transforms[id(child)] = dict(matrix=mat, wrapper_offset=n['poff'])
        if not pad_nodes:
            continue
        n_verts, node_errors, n_objects = _extract_mesh_nodes_to_objects(
            data, pad_nodes, merge_into_one=False, base_name=None,
            per_node_collection=lambda name, k=dest_key: collections[k],
            tex_metas_by_id=tex_metas_by_id, material_cache=material_cache,
            node_transforms=node_transforms)
        if node_errors:
            report({'WARNING'}, f"{len(node_errors)} {label} node(s) failed to parse and "
                                 f"were skipped, e.g.: {node_errors[0]}")
        if n_verts > 0:
            report({'INFO'}, f"Created {n_objects} {label} object(s), {n_verts} vertices total.")
        total_verts += n_verts

    return total_verts


class VEX_OT_import(bpy.types.Operator, ImportHelper):
    bl_idname = "vex.import_track"
    bl_label = "Import VEX Track"
    bl_options = {'REGISTER', 'UNDO'}

    filename_ext = ".vex"
    filter_glob: StringProperty(default="*.vex", options={'HIDDEN'})
    import_textures: BoolProperty(
        name="Import Textures",
        description="Decode real in-game textures and apply them as materials. "
                    "Slower, and creates one Blender image/material per texture "
                    "actually used - disable for a faster, geometry-only import",
        default=True,
    )

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

        tex_metas_by_id = None
        material_cache = None
        if self.import_textures:
            try:
                nodes_size = struct.unpack_from('<I', data, 4)[0]
                textures_begin = 16 + nodes_size
                tex_metas_by_id = collect_texture_metas(data, nodes, textures_begin)
                material_cache = {}
                self.report({'INFO'}, f"Found {len(tex_metas_by_id)} texture(s) in this file.")
            except Exception:
                self.report({'WARNING'}, "Texture parsing failed, continuing without "
                                          "textures:\n" + traceback.format_exc())
                tex_metas_by_id = None
                material_cache = None

        try:
            n_coll, skipped = _build_collision_objects(data, nodes, collections, self.report)
            n_verts_total += n_coll
            if skipped:
                self.report({'WARNING'}, f"Skipped non-standard collision node(s): "
                                          f"{[s[0] for s in skipped]}")
        except Exception:
            self.report({'ERROR'}, "Collision import failed:\n" + traceback.format_exc())

        try:
            n_track = _build_track_mesh_object(data, nodes, collections, self.report,
                                                tex_metas_by_id, material_cache)
            n_verts_total += n_track
        except Exception:
            self.report({'ERROR'}, "Track mesh import failed:\n" + traceback.format_exc())

        try:
            n_pads = _build_pad_objects(data, nodes, collections, self.report,
                                         tex_metas_by_id, material_cache)
            n_verts_total += n_pads
        except Exception:
            self.report({'ERROR'}, "Pad import failed:\n" + traceback.format_exc())

        # Build the WO_TRACK lane line visualization (racing line, colored per lane)
        try:
            lane_objs = build_lane_line_objects(data, nodes, collections['wo_track_lanes'])
            if lane_objs:
                self.report({'INFO'}, f"Created {len(lane_objs)} WO Track lane line(s).")
        except Exception:
            self.report({'WARNING'}, "WO Track lane line build failed:\n" + traceback.format_exc())

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
            if obj.type != 'MESH':
                continue

            # WO Track lane line (editable racing line) - patch position bytes.
            wotrack_offsets_json = obj.get('wotrack_offsets')
            if wotrack_offsets_json is not None:
                try:
                    mesh = obj.data
                    n = len(mesh.vertices)
                    orig_n = obj.get('wotrack_orig_vcount')
                    if orig_n is not None and n != orig_n:
                        errors.append(f"'{obj.name}': {n} vertices now vs {orig_n} originally - "
                                       f"did you add/remove vertices? Object skipped.")
                        continue
                    offsets = json.loads(wotrack_offsets_json)
                    if len(offsets) != n:
                        errors.append(f"'{obj.name}': wotrack_offsets has {len(offsets)} entries "
                                       f"but the mesh has {n} vertices - object skipped.")
                        continue
                    coords = [0.0] * (n * 3)
                    mesh.vertices.foreach_get('co', coords)
                    n_objects += 1
                    for i in range(n):
                        bx, by, bz = coords[i * 3], coords[i * 3 + 1], coords[i * 3 + 2]
                        world = obj.matrix_world @ Vector((bx, by, bz))
                        gx, gy, gz = blender_to_game(world.x, world.y, world.z)
                        # Write only the position (+0x00) of the WO_TRACK point.
                        struct.pack_into('<3f', data, offsets[i], gx, gy, gz)
                        n_patched += 1
                    continue
                except Exception:
                    errors.append(f"'{obj.name}': unexpected error (wotrack lane):\n"
                                   + traceback.format_exc())
                    continue

            fmt = obj.get('vex_fmt')
            if fmt is None:
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

                wrapper_offset = obj.get('vex_wrapper_offset')

                n_objects += 1
                for i in range(n):
                    bx, by, bz = coords[i * 3], coords[i * 3 + 1], coords[i * 3 + 2]
                    if wrapper_offset is None:
                        # Regular object: fold in any Object-Mode move/rotate/
                        # scale, since there's no separate wrapper transform
                        # to carry it instead.
                        world = obj.matrix_world @ Vector((bx, by, bz))
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

                if wrapper_offset is not None:
                    # This object's world position/orientation IS the
                    # wrapper's transform (see import) - write any change
                    # (from moving/rotating the object in Object Mode) back
                    # into the wrapper node's own 64-byte payload. This is
                    # what actually relocates e.g. a pad in-game, as opposed
                    # to only changing how its mesh looks.
                    new_wrapper = blender_matrix_to_wrapper(obj.matrix_world)
                    struct.pack_into('<16f', data, wrapper_offset, *new_wrapper)

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


class VEX_OT_toggle_ai_path(bpy.types.Operator):
    """Inverts the AI racing line (WO_TRACK) in the imported track file.
    Toggles between 'path: Forward' and 'path: Reverse'."""
    bl_idname = "vex.toggle_ai_path"
    bl_label = "Toggle AI Path"
    bl_description = "Invert the AI racing line (Forward <-> Reverse)"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        src_path = context.scene.get('vex_source_path')
        if not src_path:
            self.report({'ERROR'}, "No source .vex file. Import a track first.")
            return {'CANCELLED'}

        try:
            with open(src_path, 'rb') as f:
                data = bytearray(f.read())
        except OSError as e:
            self.report({'ERROR'}, f"Could not read source file: {e}")
            return {'CANCELLED'}

        try:
            root = vexx_parse_tree(data)
            nodes = vexx_flatten(root)
        except Exception as e:
            self.report({'ERROR'}, f"Could not parse VEXX tree: {e}")
            return {'CANCELLED'}

        payload = get_source_payload(data, nodes)
        if payload is None:
            self.report({'ERROR'}, "No WO_TRACK node found in this file.")
            return {'CANCELLED'}

        payload_ba, poff, plen = payload
        was_modified = reverse_wotrack_payload(payload_ba)
        if not was_modified:
            self.report({'ERROR'}, "WO_TRACK payload not recognized (bad magic).")
            return {'CANCELLED'}

        # Write modified payload back into the file
        data[poff: poff + plen] = payload_ba

        # Save to the same path (this is the source file; safe to overwrite the
        # original since we read it fresh each time).
        try:
            with open(src_path, 'wb') as f:
                f.write(data)
        except OSError as e:
            self.report({'ERROR'}, f"Could not write file: {e}")
            return {'CANCELLED'}

        # Toggle the scene flag (forward <-> reverse)
        is_reverse = not context.scene.get('vex_ai_reverse', False)
        context.scene['vex_ai_reverse'] = is_reverse

        # Reimport to refresh the lane visualization and geometry in the viewport
        try:
            bpy.ops.vex.import_track(filepath=src_path)
        except Exception:
            # Reimport is best-effort; the file on disk is already correct.
            pass

        self.report({'INFO'}, f"AI path set to {'REVERSE' if is_reverse else 'FORWARD'} "
                              f"and saved to {src_path}.")
        return {'FINISHED'}


# ============================================================================
# UI
# ============================================================================

class VEX_OT_toggle_lanes(bpy.types.Operator):
    bl_idname = "vex.toggle_lanes"
    bl_label = "Toggle WO Track Lanes"
    bl_description = "Show / hide the WO Track racing-line lane lines"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        coll = bpy.data.collections.get("VEX - WO Track Lanes")
        if coll is None:
            self.report({'INFO'}, "No WO Track Lanes collection found. Import a track first.")
            return {'CANCELLED'}
        hide = not coll.hide_viewport
        coll.hide_viewport = hide
        coll.hide_render = hide
        self.report({'INFO'}, f"WO Track Lanes {'hidden' if hide else 'shown'}.")
        return {'FINISHED'}


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

        # AI path toggle (Forward <-> Reverse)
        if src:
            box_path = layout.box()
            is_reverse = context.scene.get('vex_ai_reverse', False)
            box_path.label(text="AI path:", icon='AUTO')
            box_path.operator("vex.toggle_ai_path", icon='SWAP',
                              text="path: Reverse" if is_reverse else "path: Forward")

        # WO Track Lanes toggle
        lanes_coll = bpy.data.collections.get("VEX - WO Track Lanes")
        if lanes_coll is not None:
            box2 = layout.box()
            box2.label(text="WO Track Lanes:", icon='LIGHT')
            box2.operator("vex.toggle_lanes", icon='CHECKBOX_DEHLT',
                          text="Hidden" if lanes_coll.hide_viewport else "Visible")

        layout.separator()
        layout.label(text="Racing-line fine tuning:")
        col = layout.column(align=True)
        col.scale_y = 0.8
        col.label(text="- Edit the lane line vertices to move the AI path")
        col.label(text="- Move/rotate whole lane object in Object Mode")
        col.label(text="- Export writes the new positions back to the .vex")

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

classes = (VEX_OT_import, VEX_OT_export, VEX_OT_toggle_lanes, VEX_OT_toggle_ai_path, VEX_PT_panel)


def register():
    for c in classes:
        bpy.utils.register_class(c)


def unregister():
    for c in classes:
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()
