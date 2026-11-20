"""Drawing JSON (modelo 3D en mm) -> DXF/DWG + PNG, en vista isométrica u ortogonal."""
import math
from pathlib import Path

import ezdxf
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from ezdxf import bbox
from ezdxf.addons import odafc
from ezdxf.addons.drawing import Frontend, RenderContext, matplotlib as mpl
from ezdxf.addons.drawing.config import BackgroundPolicy, ColorPolicy, Configuration

VIEWS = ("iso", "alzado", "planta")
ROLES = {  # rol -> (capa, color ACI)
    "nueva": ("TUBERIA_NUEVA", 3),
    "existente": ("TUBERIA_EXISTENTE", 8),
    "instrumentacion": ("INSTRUMENTACION", 5),
    "demolicion": ("DEMOLICION", 1),
    "medida": ("PUNTOS_MEDIDA", 6),
}
SIZE = 150.0  # mm, tamaño base de los iconos
C30, S30 = math.cos(math.radians(30)), math.sin(math.radians(30))
MAX_ITEMS = 500

# Iconos en coordenadas locales (u = eje de la tubería, v = perpendicular). Se dibujan en 3D
# y se proyectan, así salen bien tanto en isométrico (elipses) como en ortogonal.
def _circle(cx, cy, r, n=32):
    return [(cx + r * math.cos(2 * math.pi * i / n), cy + r * math.sin(2 * math.pi * i / n)) for i in range(n)]

ICONS = {  # tipo -> [(puntos, cerrado)]
    "valve": [([(-1, -.8), (-1, .8), (1, -.8), (1, .8)], True)],
    "check_valve": [([(-1, -.8), (-1, .8), (1, 0)], True), ([(1, -.8), (1, .8)], False)],
    "gauge": [([(0, 0), (0, 2)], False), (_circle(0, 2.8, .8), True)],
    "transmitter": [([(0, 0), (0, 1.5)], False), ([(-.8, 1.5), (.8, 1.5), (.8, 3), (-.8, 3)], True)],
    "flowmeter": [([(-1.5, -1), (1.5, -1), (1.5, 1), (-1.5, 1)], True), ([(0, 1), (0, 2)], False)],
    "reducer": [([(-1, -.9), (-1, .9), (1, .4), (1, -.4)], True)],
    "flange": [([(-.2, -1), (-.2, 1)], False), ([(.2, -1), (.2, 1)], False)],
    "pump": [(_circle(0, 0, 1.3), True), ([(-.6, .9), (1.2, 0), (-.6, -.9)], False)],
    "measure_point": [(_circle(0, 0, .9), True)],
    "cross_x": [([(-1, -1), (1, 1)], False), ([(-1, 1), (1, -1)], False), (_circle(0, 0, .9), True)],
    "generic": [([(-1, -1), (1, -1), (1, 1), (-1, 1)], True)],
}
AXES = {"x": (1, 0, 0), "y": (0, 1, 0), "z": (0, 0, 1)}


def _num(v, name):
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name}: no es un número ({v!r})")
    if not math.isfinite(f):
        raise ValueError(f"{name}: no es finito")
    return f


def _pt(p, name):
    if not isinstance(p, dict):
        raise ValueError(f"{name}: punto inválido")
    return tuple(_num(p.get(k, 0), f"{name}.{k}") for k in "xyz")


def _snap(points, rel=0.03, abs_mm=40.0):
    """Tuberías: quita puntos repetidos y alinea con el eje los tramos que se desvían solo un poco (ruido de lectura)."""
    out, prev = [points[0]], points[0]
    for p in points[1:]:
        if p == prev:  # punto repetido
            continue
        prev, a = p, out[-1]
        delta = [abs(p[k] - a[k]) for k in range(3)]
        big = max(delta)
        if big == 0:
            continue
        out.append(tuple(a[k] if delta[k] < rel * big and delta[k] < abs_mm else p[k] for k in range(3)))
    return out


def _dist_seg(p, a, b):
    ab = [b[k] - a[k] for k in range(3)]
    l2 = sum(x * x for x in ab)
    t = 0.0 if l2 == 0 else max(0.0, min(1.0, sum((p[k] - a[k]) * ab[k] for k in range(3)) / l2))
    return math.dist(p, [a[k] + t * ab[k] for k in range(3)])


TOL_SOBRE_TUBERIA = {"gauge": 400.0, "transmitter": 400.0}  # los instrumentos con palito se dibujan separados de la tubería


def problems(v, tol=120.0):
    """Incoherencias geométricas del dibujo ya validado (para pedirle a la IA que lo revise). Lista de frases."""
    out = []
    segs = []
    for i, p in enumerate(v["pipes"], 1):
        for j, (a, b) in enumerate(zip(p["points"], p["points"][1:]), 1):
            segs.append((a, b))
            d = sorted(abs(b[k] - a[k]) for k in range(3))
            if d[1] > 0.03 * d[2] and d[1] > 40:
                out.append(f"El tramo {j} de la tubería {i} no es paralelo a ningún eje.")
    if segs:
        for s in v["symbols"]:
            dmin = min(_dist_seg(s["pos"], a, b) for a, b in segs)
            if dmin > TOL_SOBRE_TUBERIA.get(s["type"], tol):
                nombre = f"«{s['type']}»" + (f" {s['label']}" if s["label"] else "")
                out.append(f"El símbolo {nombre} está a {dmin:.0f} mm de cualquier tubería: debería estar sobre ella.")
    return out[:8]


def validate(d):
    """Normaliza y valida lo que devuelve la IA. Lanza ValueError con un mensaje legible."""
    if not isinstance(d, dict):
        raise ValueError("El dibujo no es un objeto JSON")
    out = {"title": str(d.get("title") or "ImageToCad")[:80],
           "view_hint": d.get("view_hint") if d.get("view_hint") in VIEWS else "iso",
           "pipes": [], "symbols": [], "texts": [], "dims": []}
    role = lambda r: r if r in ROLES else "nueva"
    for i, p in enumerate((d.get("pipes") or [])[:MAX_ITEMS]):
        pts = [_pt(q, f"pipes[{i}]") for q in p.get("points") or []]
        pts = _snap(pts) if pts else pts
        if len(pts) >= 2:
            out["pipes"].append({"points": pts, "role": role(p.get("role")), "diameter": str(p.get("diameter") or "")[:20]})
    for i, s in enumerate((d.get("symbols") or [])[:MAX_ITEMS]):
        out["symbols"].append({"type": s.get("type") if s.get("type") in ICONS else "generic",
                               "pos": _pt(s, f"symbols[{i}]"), "axis": s.get("axis") if s.get("axis") in AXES else "x",
                               "role": role(s.get("role")), "label": str(s.get("label") or "")[:60]})
    for i, t in enumerate((d.get("texts") or [])[:MAX_ITEMS]):
        if t.get("text"):
            out["texts"].append({"pos": _pt(t, f"texts[{i}]"), "text": str(t["text"])[:120]})
    for i, m in enumerate((d.get("dims") or [])[:MAX_ITEMS]):
        out["dims"].append({"p1": _pt(m.get("p1"), f"dims[{i}].p1"), "p2": _pt(m.get("p2"), f"dims[{i}].p2"),
                            "text": str(m.get("text") or "")[:40], "offset": _num(m.get("offset") or 300, "offset")})
    if not (out["pipes"] or out["symbols"]):
        raise ValueError("El dibujo no contiene tuberías ni símbolos")
    return out


def project(view, p):
    x, y, z = p
    if view == "iso":
        return ((x - y) * C30, (x + y) * S30 + z)
    return (x, z) if view == "alzado" else (x, y)


def _local_frame(axis):
    u = AXES[axis]
    v = (1, 0, 0) if axis == "z" else (0, 0, 1)
    return u, v


def _icon_points(kind, pos, axis, size=SIZE):
    u, v = _local_frame(axis)
    for pts, closed in ICONS[kind]:
        yield [tuple(pos[k] + size * (a * u[k] + b * v[k]) for k in range(3)) for a, b in pts], closed


# ---------------------------------------------------------------------------------------------------------------
# Composición: los iconos se escalan según el espacio que tienen y las etiquetas/cotas se colocan sin pisarse.
# Todo se calcula en 2D, ya proyectado a la vista que se dibuja.
RADIO = {"valve": 1.1, "check_valve": 1.1, "gauge": 2.0, "transmitter": 1.7, "flowmeter": 1.6, "reducer": 1.1,
         "flange": 1.1, "pump": 1.4, "measure_point": 1.0, "cross_x": 1.1, "generic": 1.2}  # radio aproximado, en unidades de tamaño
ANCHO_CAR = 0.74  # ancho medio de un carácter (mayúsculas incluidas) respecto a la altura del texto


def _rect_hit(r1, r2, pad=0.0):
    return not (r1[2] + pad <= r2[0] or r2[2] + pad <= r1[0] or r1[3] + pad <= r2[1] or r2[3] + pad <= r1[1])


def _rect_seg_hit(r, a, b, pad=0.0):
    """¿El segmento a-b toca el rectángulo r? (recorte de Liang-Barsky)"""
    x0, y0, x1, y1 = r[0] - pad, r[1] - pad, r[2] + pad, r[3] + pad
    dx, dy, t0, t1 = b[0] - a[0], b[1] - a[1], 0.0, 1.0
    for p, q in ((-dx, a[0] - x0), (dx, x1 - a[0]), (-dy, a[1] - y0), (dy, y1 - a[1])):
        if p == 0:
            if q < 0:
                return False
        else:
            t = q / p
            if p < 0:
                t0 = max(t0, t)
            else:
                t1 = min(t1, t)
            if t0 > t1:
                return False
    return True


def _dist_seg2(p, a, b):
    ab = (b[0] - a[0], b[1] - a[1])
    l2 = ab[0] ** 2 + ab[1] ** 2
    t = 0.0 if l2 == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * ab[0] + (p[1] - a[1]) * ab[1]) / l2))
    return math.dist(p, (a[0] + t * ab[0], a[1] + t * ab[1]))


def layout(d, view):
    """Tamaños de iconos y posición de etiquetas, diámetros, textos y cotas para que quepan sin superponerse."""
    P = lambda p: project(view, p)
    segs = [(P(a), P(b)) for p in d["pipes"] for a, b in zip(p["points"], p["points"][1:]) if P(a) != P(b)]
    pos2 = [P(s["pos"]) for s in d["symbols"]]
    pts = [q for sg in segs for q in sg] + pos2 or [(0.0, 0.0), (1.0, 1.0)]
    xs, ys = zip(*pts)
    ext = max(max(xs) - min(xs), max(ys) - min(ys), 1.0)
    S0 = max(ext / 30, 25.0)  # tamaño base de los iconos; el texto es algo menor
    h = S0 * 0.55

    # 1) tamaño de cada icono: no más grande que el hueco hasta su vecino ni que el tramo donde está
    sizes = []
    for i, s in enumerate(d["symbols"]):
        r, size = RADIO[s["type"]], S0
        for j, t in enumerate(d["symbols"]):
            dist = math.dist(pos2[i], pos2[j]) if j != i else 0
            if j != i and dist > 1e-6:
                size = min(size, 0.9 * dist / (r + RADIO[t["type"]]))
        if segs:
            a, b = min(segs, key=lambda sg: _dist_seg2(pos2[i], *sg))
            if _dist_seg2(pos2[i], a, b) < 0.5 * S0 and math.dist(a, b) > 0:
                size = min(size, math.dist(a, b) / (2 * r))
        sizes.append(max(size, S0 * 0.3))
    icon_rects = []
    for s, size in zip(d["symbols"], sizes):
        q = [P(p) for pts3, _ in _icon_points(s["type"], s["pos"], s["axis"], size) for p in pts3]
        qx, qy = zip(*q)
        icon_rects.append((min(qx), min(qy), max(qx), max(qy)))

    # 2) textos: cada uno prueba varias posiciones alrededor de su ancla (cada vez más lejos) y se queda con la primera libre
    placed, texts = [], []

    def colisiones(rect):
        n = sum(_rect_hit(rect, r, h * 0.3) for r in icon_rects) + sum(_rect_hit(rect, r, h * 0.4) for r in placed)
        return n + sum(_rect_seg_hit(rect, a, b, h * 0.1) for a, b in segs)

    def colocar(texto, ancla, hueco, alto, layer, directo=False):
        w, hh = len(texto) * alto * ANCHO_CAR, alto * 1.15
        cands = [(0.0, 0.0, 0)] if directo else []
        for k, m in enumerate((1.0, 1.8, 2.8, 4.0, 6.0, 9.0)):
            g = hueco * m
            cands += [(dx, dy, k) for dx, dy in ((g, g), (g, -g - hh), (-g - w, g), (-g - w, -g - hh), (-w / 2, g + hh * .2),
                                                 (-w / 2, -g - hh), (g, -hh / 2), (-g - w, -hh / 2))]
        mejor = None
        for dx, dy, k in cands:
            rect = (ancla[0] + dx, ancla[1] + dy, ancla[0] + dx + w, ancla[1] + dy + hh)
            n = colisiones(rect)
            if mejor is None or n < mejor[0]:
                mejor = (n, rect, k, (dx, dy))
            if n == 0:
                break
        n, rect, k, (dx, dy) = mejor
        placed.append(rect)
        texts.append({"text": texto, "xy": (rect[0], rect[1]), "layer": layer, "height": alto,
                      "leader": (ancla, (rect[0], rect[1])) if k >= 2 else None})

    for s, p2, size in zip(d["symbols"], pos2, sizes):
        if s["label"]:
            colocar(s["label"], p2, size * RADIO[s["type"]] + h * 0.3, h * 0.8, ROLES[s["role"]][0])
    for p in d["pipes"]:
        if p["diameter"]:
            sg = [(P(a), P(b)) for a, b in zip(p["points"], p["points"][1:]) if P(a) != P(b)]
            if sg:
                a, b = max(sg, key=lambda x: math.dist(*x))
                colocar(p["diameter"], ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2), h * 0.4, h * 0.8, ROLES[p["role"]][0])
    for t in d["texts"]:
        colocar(t["text"], P(t["pos"]), h * 0.4, h, "TEXTO", directo=True)

    # 3) cotas: la línea de cota se aparta de la tubería y de lo ya dibujado, y las cotas entre sí
    dims, lines = [], []
    for m in d["dims"]:
        a, b = P(m["p1"]), P(m["p2"])
        if a == b:
            dims.append(None)
            continue
        L = math.dist(a, b)
        n = (-(b[1] - a[1]) / L, (b[0] - a[0]) / L)  # normal izquierda: el signo del offset elige el lado
        sign = 1 if m["offset"] >= 0 else -1
        w = len(m["text"] or f"{math.dist(m['p1'], m['p2']):.0f}") * h * ANCHO_CAR
        elegido = None
        for k in range(10):
            off = sign * (max(abs(m["offset"]), 2.5 * h) + k * 2.2 * h)
            la, lb = (a[0] + n[0] * off, a[1] + n[1] * off), (b[0] + n[0] * off, b[1] + n[1] * off)
            mid = ((la[0] + lb[0]) / 2, (la[1] + lb[1]) / 2)
            caja = (mid[0] - w / 2, mid[1] - h, mid[0] + w / 2, mid[1] + h)
            muestras = [(la[0] + (lb[0] - la[0]) * t / 8, la[1] + (lb[1] - la[1]) * t / 8) for t in range(9)]
            malo = (any(_rect_seg_hit(r, la, lb, h * 0.3) for r in icon_rects + placed)
                    or any(_rect_hit(caja, r, h * 0.2) for r in icon_rects + placed)
                    or any(_dist_seg2(q, *sg) < 1.1 * h for q in muestras for sg in segs)
                    or any(math.dist(q, z) < 1.8 * h for q in muestras for ln in lines for z in ln))
            elegido = off
            if not malo:
                break
        lines.append(muestras)
        placed.append(caja)
        dims.append(elegido)
    return {"h": h, "S0": S0, "sizes": sizes, "icon_rects": icon_rects, "texts": texts, "dims": dims}


def build(drawing, view="iso"):
    """Devuelve un ezdxf Drawing con el dibujo proyectado en la vista pedida."""
    d = validate(drawing)
    view = view if view in VIEWS else d["view_hint"]
    doc = ezdxf.new("R2010", setup=True)
    doc.units = ezdxf.units.MM
    for layer, color in ROLES.values():
        doc.layers.add(layer, color=color)
    doc.layers.add("COTAS", color=7)
    doc.layers.add("TEXTO", color=7)
    msp = doc.modelspace()
    P = lambda p: project(view, p)

    lay = layout(d, view)
    h = lay["h"]

    def text(s, xy, layer="TEXTO", height=h):
        msp.add_text(s, height=height, dxfattribs={"layer": layer, "insert": xy})

    for p in d["pipes"]:
        layer = ROLES[p["role"]][0]
        pts = [P(q) for q in p["points"]]
        pts = [a for i, a in enumerate(pts) if i == 0 or a != pts[i - 1]]  # segmentos degenerados (eje de vista)
        if len(pts) >= 2:
            msp.add_lwpolyline(pts, dxfattribs={"layer": layer, "const_width": h * 0.15})
    for s, size in zip(d["symbols"], lay["sizes"]):
        layer = ROLES[s["role"]][0]
        for pts, closed in _icon_points(s["type"], s["pos"], s["axis"], size):
            msp.add_lwpolyline([P(q) for q in pts], close=closed, dxfattribs={"layer": layer})
    for t in lay["texts"]:
        text(t["text"], t["xy"], t["layer"], t["height"])
        if t["leader"]:  # el texto quedó lejos de su elemento: línea fina que los une
            msp.add_line(*t["leader"], dxfattribs={"layer": "TEXTO"})
    for m, off in zip(d["dims"], lay["dims"]):
        if off is None:
            continue
        a, b = P(m["p1"]), P(m["p2"])
        length = math.dist(m["p1"], m["p2"])  # medida real 3D aunque la vista la acorte
        msp.add_aligned_dim(p1=a, p2=b, distance=off, text=m["text"] or f"{length:.0f}",
                            dxfattribs={"layer": "COTAS"},
                            override={"dimtxt": h, "dimasz": h * .8, "dimexe": h * .4, "dimexo": h * .4,
                                      "dimlfac": 1, "dimdec": 0, "dimclrt": 7}).render()
    return doc


def render_png(doc, path, width_in=13, dpi=130):
    ext = bbox.extents(doc.modelspace())
    (x0, y0, _), (x1, y1, _) = ext.extmin, ext.extmax
    mx, my = (x1 - x0) * .06 + 50, (y1 - y0) * .06 + 50
    x0, x1, y0, y1 = x0 - mx, x1 + mx, y0 - my, y1 + my
    fig = plt.figure(figsize=(width_in, max(3, min(width_in * (y1 - y0) / (x1 - x0), 12))), dpi=dpi)
    try:
        ax = fig.add_axes([0, 0, 1, 1])
        cfg = Configuration(background_policy=BackgroundPolicy.WHITE, color_policy=ColorPolicy.COLOR)
        Frontend(RenderContext(doc), mpl.MatplotlibBackend(ax), config=cfg).draw_layout(doc.modelspace())
        ax.set_xlim(x0, x1); ax.set_ylim(y0, y1); ax.set_aspect("equal")
        fig.savefig(path, facecolor="white")
    finally:
        plt.close(fig)  # pyplot no es thread-safe: el llamador serializa con un lock


def set_oda_path(path):
    if path:
        ezdxf.options.set("odafc-addon", "unix_exec_path", path)
        ezdxf.options.set("odafc-addon", "win_exec_path", path)


def dwg_available():
    return odafc.is_installed()


def save(doc, path):
    """Guarda por extensión: .dxf siempre; .dwg vía ODA File Converter."""
    path = Path(path)
    if path.suffix == ".dwg":
        odafc.export_dwg(doc, str(path), version="R2018", replace=True)
    else:
        doc.saveas(path)
