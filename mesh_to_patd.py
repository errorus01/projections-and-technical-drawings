"""
Blueprint sheet generator v4 (Blender 4.x / 5.x). Запускать в обычном окне Blender.

MODE = "hard"    -- техника: только чистые линии Freestyle (контур, острые рёбра, границы материалов).
MODE = "hard+"   -- как hard + линии СТЫКОВ (где детали входят друг в друга/в корпус):
                    по ID объектов, изломам нормалей и уступам глубины. Рекомендуется для техники.
MODE = "organic" -- гладкие формы (голова, животные): Freestyle + края по нормалям/глубине.

Соглашение: перед модели смотрит в -Y, длинная ось -- Y.
"""
import bpy
import math
import os
import numpy as np
from mathutils import Vector

# ----------------------------- НАСТРОЙКИ -----------------------------
MODE = "hard+"            # "hard" | "hard+" | "organic"
MODEL_PATH = ""
OUT_DIR = "C:/blueprint/"   # сюда пишутся лист, виды и passes/
SHEET_NAME = "sheet.png"

LONG_SIDE_PX = 4000       # пикселей на самую длинную сторону модели (итоговое разрешение)
MARGIN = 1.04
SS = 2                    # суперсэмплинг: рендер в SS раз больше, потом уменьшаем (гладкие тонкие линии)

# --- толщина линий в пикселях ИТОГОВОГО изображения (можно дробную) ---
OUTLINE_PX = 0.9          # внешний контур и границы
DETAIL_PX = 0.55          # внутренние линии
LINE_CONTRAST = 1.5       # 1.0 = как есть; выше = тонкие линии темнее (не шире)

# --- Freestyle ---
CREASE_ANGLE = 35         # градусы; больше -> меньше мелких линий (25..60)
MIN_LINE_PX = 0           # >0: отсекать короткие обрывки (в пикселях рендера; пробуй 20..60). 0 = выкл.

# --- только для MODE = "hard+" (линии стыков) ---
JOINT_NORMAL_ANGLE = 30.0   # излом поверхности, градусы. Меньше -> больше линий стыков (20..50)
JOINT_DEPTH_JUMP = 0.012    # уступ глубины, доля размера модели. Меньше -> больше линий
JOINT_SPAN = 2              # масштаб в px (при SS). Больше -> грубее, но меньше шума
JOINT_USE_ID = True         # границы между отдельными объектами сцены
DEDUP_PX = 1.5              # не дублировать линии Freestyle (радиус, итоговые px)

# --- только для MODE = "organic" ---
NORMAL_ANGLE = 14.0
NORMAL_ANGLE_STRONG = 28.0
SPAN = 3
NORMAL_BLUR = 2
DEPTH_JUMP = 0.025
SMOOTH_OUTLINE = 3
INNER_MARGIN = 3

LEFT_COL = ["right", "top", "left"]
RIGHT_COL = ["front", "back"]
GAP = 60
FRAME_PX = 2

VIEWS = {
    "front":  ((90, 0, 0),   Vector((0, -1, 0)), (0, 2)),
    "back":   ((90, 0, 180), Vector((0, 1, 0)),  (0, 2)),
    "right":  ((90, 0, 90),  Vector((1, 0, 0)),  (1, 2)),
    "left":   ((90, 0, -90), Vector((-1, 0, 0)), (1, 2)),
    "top":    ((0, 0, 90),   Vector((0, 0, 1)),  (1, 0)),
    "bottom": ((180, 0, 90), Vector((0, 0, -1)), (1, 0)),
}


# ----------------------------- сцена -----------------------------
def import_model(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".obj":
        bpy.ops.wm.obj_import(filepath=path)
    elif ext == ".fbx":
        bpy.ops.import_scene.fbx(filepath=path)
    elif ext in (".glb", ".gltf"):
        bpy.ops.import_scene.gltf(filepath=path)
    else:
        raise ValueError("Unsupported format: " + ext)


def world_bbox(meshes):
    dg = bpy.context.evaluated_depsgraph_get()
    mn = Vector((1e18,) * 3)
    mx = Vector((-1e18,) * 3)
    for o in meshes:
        eo = o.evaluated_get(dg)
        for c in eo.bound_box:
            w = eo.matrix_world @ Vector(c)
            for i in range(3):
                mn[i] = min(mn[i], w[i])
                mx[i] = max(mx[i], w[i])
    return mn, mx


def eevee_id():
    ids = [e.identifier for e in bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items]
    for n in ("BLENDER_EEVEE", "BLENDER_EEVEE_NEXT"):
        if n in ids:
            return n
    raise RuntimeError("Eevee not found")


def make_lineset(vl, name, px, **flags):
    ls = vl.freestyle_settings.linesets.new(name)
    ls.select_by_visibility = True
    ls.visibility = "VISIBLE"
    for attr in ("select_silhouette", "select_border", "select_crease", "select_contour",
                 "select_material_boundary", "select_edge_mark", "select_external_contour",
                 "select_suggestive_contour", "select_ridge_valley"):
        setattr(ls, attr, flags.get(attr, False))
    ls.select_by_edge_types = True
    if MIN_LINE_PX > 0:
        ls.select_by_length = True
        ls.length_min = MIN_LINE_PX
        ls.length_max = 1.0e7
    ls.linestyle = bpy.data.linestyles.new(name)
    ls.linestyle.color = (0, 0, 0)
    ls.linestyle.thickness = px * SS   # рендерим в SS раз крупнее
    ls.linestyle.thickness_position = "CENTER"


def set_world(sc, white):
    v = 1.0 if white else 0.0
    sc.world.color = (v, v, v)
    try:
        bg = sc.world.node_tree.nodes.get("Background")
        if bg:
            bg.inputs[0].default_value = (v, v, v, 1)
            bg.inputs[1].default_value = v
    except Exception:
        pass


def setup_scene(meshes):
    sc = bpy.data.scenes.new("blueprint_tmp")
    for o in meshes:
        sc.collection.objects.link(o)

    r = sc.render
    r.film_transparent = False
    r.image_settings.file_format = "PNG"
    r.image_settings.color_mode = "RGB"
    r.resolution_percentage = 100
    r.line_thickness_mode = "ABSOLUTE"
    r.dither_intensity = 0.0
    sc.display_settings.display_device = "sRGB"

    sh = sc.display.shading
    sh.light = "FLAT"
    sh.color_type = "SINGLE"
    sh.single_color = (1, 1, 1)
    sh.show_shadows = False
    sh.show_cavity = False
    sh.show_object_outline = False

    world = bpy.data.worlds.new("bp_world")
    try:
        world.use_nodes = True
    except Exception:
        pass
    sc.world = world
    set_world(sc, True)

    vl = sc.view_layers[0]
    vl.use_freestyle = True
    fs = vl.freestyle_settings
    for ls in list(fs.linesets):
        fs.linesets.remove(ls)
    fs.crease_angle = math.radians(CREASE_ANGLE)
    if MODE in ("hard", "hard+"):
        make_lineset(vl, "outline", OUTLINE_PX, select_silhouette=True, select_border=True)
        make_lineset(vl, "detail", DETAIL_PX, select_crease=True,
                     select_material_boundary=True, select_edge_mark=True)
    else:
        make_lineset(vl, "detail", DETAIL_PX,
                     select_material_boundary=True, select_edge_mark=True)
    return sc


def set_transform(sc, raw):
    vs = sc.view_settings
    ok = True
    if raw:
        try:
            vs.view_transform = "Raw"
        except Exception:
            vs.view_transform = "Standard"
            ok = False
    else:
        vs.view_transform = "Standard"
    try:
        vs.look = "None"
    except Exception:
        pass
    return ok


# ----------------------------- материалы для проходов (organic) -----------------------------
def make_normal_material():
    m = bpy.data.materials.new("bp_normal")
    m.use_nodes = True
    nt = m.node_tree
    nt.nodes.clear()
    geo = nt.nodes.new("ShaderNodeNewGeometry")
    mad = nt.nodes.new("ShaderNodeVectorMath")
    mad.operation = "MULTIPLY_ADD"
    mad.inputs[1].default_value = (0.5, 0.5, 0.5)
    mad.inputs[2].default_value = (0.5, 0.5, 0.5)
    em = nt.nodes.new("ShaderNodeEmission")
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    nt.links.new(geo.outputs["Normal"], mad.inputs[0])
    nt.links.new(mad.outputs[0], em.inputs["Color"])
    nt.links.new(em.outputs[0], out.inputs["Surface"])
    return m


def make_depth_material():
    m = bpy.data.materials.new("bp_depth")
    m.use_nodes = True
    nt = m.node_tree
    nt.nodes.clear()
    geo = nt.nodes.new("ShaderNodeNewGeometry")
    sub = nt.nodes.new("ShaderNodeVectorMath")
    sub.operation = "SUBTRACT"
    dot = nt.nodes.new("ShaderNodeVectorMath")
    dot.operation = "DOT_PRODUCT"
    mr = nt.nodes.new("ShaderNodeMapRange")
    mr.clamp = True
    em = nt.nodes.new("ShaderNodeEmission")
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    nt.links.new(geo.outputs["Position"], sub.inputs[0])
    nt.links.new(sub.outputs[0], dot.inputs[0])
    nt.links.new(dot.outputs["Value"], mr.inputs["Value"])
    nt.links.new(mr.outputs["Result"], em.inputs["Color"])
    nt.links.new(em.outputs[0], out.inputs["Surface"])
    return m, sub, dot, mr


def make_id_material():
    """Каждый объект -> свой псевдослучайный цвет."""
    m = bpy.data.materials.new("bp_id")
    m.use_nodes = True
    nt = m.node_tree
    nt.nodes.clear()
    info = nt.nodes.new("ShaderNodeObjectInfo")
    comb = nt.nodes.new("ShaderNodeCombineColor")
    for i, k in enumerate((7.31, 13.17, 3.77)):
        mul = nt.nodes.new("ShaderNodeMath")
        mul.operation = "MULTIPLY"
        mul.inputs[1].default_value = k
        fr = nt.nodes.new("ShaderNodeMath")
        fr.operation = "FRACT"
        nt.links.new(info.outputs["Random"], mul.inputs[0])
        nt.links.new(mul.outputs[0], fr.inputs[0])
        nt.links.new(fr.outputs[0], comb.inputs[i])
    em = nt.nodes.new("ShaderNodeEmission")
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    nt.links.new(comb.outputs[0], em.inputs["Color"])
    nt.links.new(em.outputs[0], out.inputs["Surface"])
    return m


# ----------------------------- рендер -----------------------------
def render_pass(sc, path, resx, resy, engine, freestyle, data_pass, override):
    r = sc.render
    r.engine = engine
    r.use_freestyle = freestyle
    r.resolution_x, r.resolution_y = resx, resy
    r.image_settings.color_depth = "16" if data_pass else "8"
    r.filter_size = 0.5 if data_pass else 0.8
    sc.view_layers[0].material_override = override
    set_world(sc, not data_pass)       # линии: белый фон; карты: чёрный фон
    raw_ok = set_transform(sc, data_pass)
    if hasattr(sc, "eevee") and hasattr(sc.eevee, "taa_render_samples"):
        sc.eevee.taa_render_samples = 8
    r.filepath = path
    bpy.ops.render.render(write_still=True, scene=sc.name)
    return raw_ok


def make_camera(sc, name, center, dims, ppu, resx, resy):
    rot, direction, _ = VIEWS[name]
    cd = bpy.data.cameras.new("cam_" + name)
    cd.type = "ORTHO"
    cd.ortho_scale = max(resx, resy) / ppu
    cd.sensor_fit = "AUTO"
    cd.clip_start = 0.01
    cd.clip_end = max(dims) * 20
    cam = bpy.data.objects.new("cam_" + name, cd)
    sc.collection.objects.link(cam)
    cam.rotation_euler = [math.radians(a) for a in rot]
    cam.location = center + direction * max(dims) * 5
    sc.camera = cam
    return cam, cd


# ----------------------------- картинки (без питоновских списков) -----------------------------
def load_img(path, gray=False):
    img = bpy.data.images.load(path)
    img.colorspace_settings.name = "Non-Color"
    w, h = img.size
    buf = np.empty(w * h * 4, np.float32)
    img.pixels.foreach_get(buf)
    bpy.data.images.remove(img)
    arr = buf.reshape(h, w, 4)
    arr = arr[::-1, :, 0] if gray else arr[::-1, :, :3]
    return np.ascontiguousarray(arr)


def save_png(arr, path):
    """arr: HxWx3 float32 (0..1), верх картинки = первая строка."""
    h, w = arr.shape[:2]
    rgba = np.ones((h, w, 4), np.float32)
    rgba[:, :, :3] = arr[::-1]
    img = bpy.data.images.new("tmp_out", w, h, alpha=False)
    img.colorspace_settings.name = "Non-Color"
    img.pixels.foreach_set(rgba.ravel())
    img.filepath_raw = path
    img.file_format = "PNG"
    img.save()
    bpy.data.images.remove(img)


def downsample(a, k):
    H, W = a.shape[:2]
    return a.reshape(H // k, k, W // k, k).mean(axis=(1, 3))


# ----------------------------- organic: края по картам -----------------------------
def srgb_to_linear(a):
    return np.where(a <= 0.04045, a / 12.92, ((a + 0.055) / 1.055) ** 2.4)


def shifted(a, dy, dx):
    H, W = a.shape[:2]
    py, px = abs(dy), abs(dx)
    p = np.pad(a, ((py, py), (px, px)) + ((0, 0),) * (a.ndim - 2), mode="edge")
    return p[py + dy:py + dy + H, px + dx:px + dx + W]


def box_blur(a, n):
    for _ in range(n):
        a = sum(shifted(a, dy, dx) for dy in (-1, 0, 1) for dx in (-1, 0, 1)) / 9.0
    return a


def erode(a, n):
    for _ in range(n):
        a = np.minimum.reduce([shifted(a, dy, dx) for dy in (-1, 0, 1) for dx in (-1, 0, 1)])
    return a


def thin(m, thr):
    l, r = shifted(m, 0, -1), shifted(m, 0, 1)
    u, d = shifted(m, -1, 0), shifted(m, 1, 0)
    keep = ((m >= l) & (m >= r)) | ((m >= u) & (m >= d))
    return keep & (m > thr)


def dilate_bool(b):
    f = b.astype(np.float32)
    f = np.maximum.reduce([shifted(f, 0, -1), f, shifted(f, 0, 1)])
    f = np.maximum.reduce([shifted(f, -1, 0), f, shifted(f, 1, 0)])
    return f > 0.5


def hysteresis(m, lo, hi, iters=60):
    weak = thin(m, lo)
    cur = thin(m, hi) & weak
    for _ in range(iters):
        nxt = dilate_bool(cur) & weak
        if np.array_equal(nxt, cur):
            break
        cur = nxt
    return cur


def normal_edge_strength(n, span=None):
    span = SPAN if span is None else span
    e = np.zeros(n.shape[:2], np.float32)
    for dy, dx in ((0, span), (span, 0), (span, span), (span, -span)):
        a, b = shifted(n, dy, dx), shifted(n, -dy, -dx)
        e = np.maximum(e, 1.0 - (a * b).sum(axis=-1))
    return e


def depth_edge_strength(d, span=None):
    span = SPAN if span is None else span
    e = np.zeros(d.shape, np.float32)
    for dy, dx in ((0, span), (span, 0), (span, span), (span, -span)):
        e = np.maximum(e, np.abs(shifted(d, dy, dx) - shifted(d, -dy, -dx)))
    return e


def feature_lines(nrm, dep, raw_ok):
    """Линии 0..1 в итоговом разрешении (вход в SS-разрешении)."""
    if not raw_ok:
        nrm = srgb_to_linear(nrm)
        dep = srgb_to_linear(dep)
    n = nrm * 2.0 - 1.0
    if NORMAL_BLUR > 0:
        n = box_blur(n, NORMAL_BLUR)
    n = n / (np.linalg.norm(n, axis=-1, keepdims=True) + 1e-6)

    mask = (dep > 0.075).astype(np.float32)
    if SMOOTH_OUTLINE > 0:
        mask = box_blur(mask, SMOOTH_OUTLINE)
    body = mask > 0.5

    t_out = max(1, int(round(OUTLINE_PX * SS)))
    er = erode(body.astype(np.float32), t_out)
    outline = body & (er < 0.5)
    inner = erode(er, INNER_MARGIN) > 0.5

    lo_n = 1.0 - math.cos(math.radians(NORMAL_ANGLE))
    hi_n = 1.0 - math.cos(math.radians(NORMAL_ANGLE_STRONG))
    lo_d = 0.85 * DEPTH_JUMP
    inner_lines = (hysteresis(normal_edge_strength(n), lo_n, hi_n)
                   | hysteresis(depth_edge_strength(dep), lo_d, 2 * lo_d)) & inner

    lines = np.maximum(inner_lines.astype(np.float32), outline.astype(np.float32))
    return downsample(lines, SS)


def chunked_rows(fn, arrs, halo, chunk=1024):
    """Применяет fn к полосам строк (с запасом halo), чтобы не раздувать память."""
    H = arrs[0].shape[0]
    out = None
    for r0 in range(0, H, chunk):
        r1 = min(H, r0 + chunk)
        a0, a1 = max(0, r0 - halo), min(H, r1 + halo)
        res = fn(*[a[a0:a1] for a in arrs])
        part = res[r0 - a0:r0 - a0 + (r1 - r0)]
        if out is None:
            out = np.empty((H,) + part.shape[1:], part.dtype)
        out[r0:r1] = part
    return out


def id_edge_strength(c):
    e = np.zeros(c.shape[:2], np.float32)
    for dy, dx in ((0, 1), (1, 0), (1, 1), (1, -1)):
        e = np.maximum(e, np.abs(shifted(c, dy, dx) - shifted(c, -dy, -dx)).sum(axis=-1))
    return e


def dilate_n(b, n):
    for _ in range(n):
        b = dilate_bool(b)
    return b


def joint_lines(nrm, dep, ids, fs_dark, raw_ok):
    """Линии стыков (SS-разрешение на входе) -> 0..1 в итоговом разрешении."""
    if not raw_ok:
        nrm, dep = srgb_to_linear(nrm), srgb_to_linear(dep)
        if ids is not None:
            ids = srgb_to_linear(ids)
    halo = JOINT_SPAN + 2

    def ne_fn(a):
        n = a * 2.0 - 1.0
        n = n / (np.linalg.norm(n, axis=-1, keepdims=True) + 1e-6)
        return normal_edge_strength(n, JOINT_SPAN)

    thr_n = 1.0 - math.cos(math.radians(JOINT_NORMAL_ANGLE))
    lines = thin(chunked_rows(ne_fn, [nrm], halo), thr_n)
    lines |= thin(chunked_rows(lambda d: depth_edge_strength(d, JOINT_SPAN), [dep], halo),
                  0.85 * JOINT_DEPTH_JUMP)
    if JOINT_USE_ID and ids is not None:
        lines |= thin(chunked_rows(id_edge_strength, [ids], 2), 0.2)

    # без дублей: не рисуем у внешнего контура и рядом с линиями Freestyle
    bg = dilate_n(dep < 0.075, 3)
    near_fs = dilate_n(fs_dark, max(1, int(round(DEDUP_PX * SS))))
    lines &= ~(bg | near_fs)
    return downsample(lines.astype(np.float32), SS)


# ----------------------------- лист -----------------------------
def add_frame(arr, t):
    arr[:t, :], arr[-t:, :], arr[:, :t], arr[:, -t:] = 0, 0, 0, 0
    return arr


def stack_column(tiles):
    width = max(t.shape[1] for t in tiles)
    height = sum(t.shape[0] for t in tiles) + GAP * (len(tiles) - 1)
    col = np.ones((height, width, 3), np.float32)
    y = 0
    for t in tiles:
        x = (width - t.shape[1]) // 2
        col[y:y + t.shape[0], x:x + t.shape[1]] = t
        y += t.shape[0] + GAP
    return col


# ----------------------------- main -----------------------------
def main():
    if MODEL_PATH:
        import_model(MODEL_PATH)
    meshes = [o for o in bpy.context.scene.objects if o.type == "MESH" and o.visible_get()]
    if not meshes:
        raise RuntimeError("В сцене нет видимых мешей")

    mn, mx = world_bbox(meshes)
    center = (mn + mx) / 2
    dims = [mx[i] - mn[i] for i in range(3)]
    ppu = LONG_SIDE_PX / max(dims)
    R = 0.5 * math.sqrt(sum(d * d for d in dims))
    print("Mode:", MODE, " Dimensions (X,Y,Z):", [round(d, 3) for d in dims])

    out_dir = bpy.path.abspath(OUT_DIR)
    pass_dir = os.path.join(out_dir, "passes")
    os.makedirs(pass_dir, exist_ok=True)

    sc = setup_scene(meshes)
    organic = MODE == "organic"
    use_passes = MODE in ("hard+", "organic")
    if use_passes:
        eevee = eevee_id()
        mat_n = make_normal_material()
        mat_d, d_sub, d_dot, d_map = make_depth_material()
        mat_i = make_id_material()

    tiles = {}
    for name in sorted(set(LEFT_COL + RIGHT_COL)):
        _, direction, (aw, ah) = VIEWS[name]
        resx = int(math.ceil(dims[aw] * MARGIN * ppu))
        resy = int(math.ceil(dims[ah] * MARGIN * ppu))
        cam, cd = make_camera(sc, name, center, dims, ppu, resx, resy)

        p_fs = os.path.join(pass_dir, f"fs_{name}.png")
        render_pass(sc, p_fs, resx * SS, resy * SS, "BLENDER_WORKBENCH", True, False, None)
        fs_ss = load_img(p_fs, gray=True)
        lines = np.clip((1.0 - downsample(fs_ss, SS)) * LINE_CONTRAST, 0.0, 1.0)

        if use_passes:
            d_sub.inputs[1].default_value = center[:]
            d_dot.inputs[1].default_value = direction[:]
            d_map.inputs["From Min"].default_value = -R
            d_map.inputs["From Max"].default_value = R
            d_map.inputs["To Min"].default_value = 0.15
            d_map.inputs["To Max"].default_value = 1.0
            p_n = os.path.join(pass_dir, f"normal_{name}.png")
            p_d = os.path.join(pass_dir, f"depth_{name}.png")
            raw_ok = render_pass(sc, p_n, resx * SS, resy * SS, eevee, False, True, mat_n)
            render_pass(sc, p_d, resx * SS, resy * SS, eevee, False, True, mat_d)
            if organic:
                feat = feature_lines(load_img(p_n), load_img(p_d, gray=True), raw_ok)
            else:
                ids = None
                if JOINT_USE_ID:
                    p_i = os.path.join(pass_dir, f"id_{name}.png")
                    render_pass(sc, p_i, resx * SS, resy * SS, eevee, False, True, mat_i)
                    ids = load_img(p_i)
                feat = joint_lines(load_img(p_n), load_img(p_d, gray=True), ids,
                                   fs_ss < 0.7, raw_ok)
                del ids
            lines = np.maximum(lines, np.clip(feat * LINE_CONTRAST, 0.0, 1.0))
            del feat
        del fs_ss

        print(f"{name}: {resx}x{resy}px, line coverage {100 * lines.mean():.2f}%")
        gray = 1.0 - lines
        tile = np.repeat(gray[:, :, None], 3, axis=2).astype(np.float32)
        save_png(tile, os.path.join(out_dir, f"view_{name}.png"))
        tiles[name] = add_frame(tile, FRAME_PX)

        sc.collection.objects.unlink(cam)
        bpy.data.objects.remove(cam)
        bpy.data.cameras.remove(cd)

    left = stack_column([tiles[n] for n in LEFT_COL])
    right = stack_column([tiles[n] for n in RIGHT_COL])
    H = max(left.shape[0], right.shape[0]) + GAP * 2
    W = left.shape[1] + right.shape[1] + GAP * 3
    sheet = np.ones((H, W, 3), np.float32)
    sheet[GAP:GAP + left.shape[0], GAP:GAP + left.shape[1]] = left
    x2 = GAP * 2 + left.shape[1]
    sheet[GAP:GAP + right.shape[0], x2:x2 + right.shape[1]] = right
    save_png(sheet, os.path.join(out_dir, SHEET_NAME))

    if use_passes:
        for m in (mat_n, mat_d, mat_i):
            bpy.data.materials.remove(m)
    bpy.data.scenes.remove(sc)
    print("Done:", os.path.join(out_dir, SHEET_NAME), f"({W}x{H})")


if __name__ == "__main__":
    main()