r"""Temperature tower geometry, built from simple convex solids.

One floor, seen from the front (x right, z up; depth runs along +y):

      ____________________________________________________________
      \      |  230   |=============== bridge ===========|  |      /
   45° \     | label  |                                  |B |     / 35°
        \    | block  |     ^             ^              |  |    /
         \   |   A    |    / \           / \             |()|   /
          \__|________|___/___\_________/___\____________|__|__/
                       stringing cones      horizontal hole ()

Solids that belong together overlap slightly and are stored as separate
closed shells in one mesh per floor; the slicer unions them. Holes and the
engraved numbers are negative volumes, so QIDI Studio does the cutting.
"""
import base64
import json
import math
import os
import struct
import sys

# Inside a PyInstaller bundle the data files are unpacked to sys._MEIPASS.
HERE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
DIGITS = os.path.join(HERE, "assets", "digits.json")

FLOOR_H = 10.0       # height of one temperature step
BASE_H = 1.0         # base plate thickness
DEPTH = 10.0         # tower depth (y)
BLOCK_W = 20.0       # label block width
BRIDGE = 30.0        # bridge span between the label block and pillar B
PILLAR_W = 10.0      # pillar B width
BRIDGE_T = 1.0       # bridge thickness
OVERHANG_LEFT = 45.0   # degrees from horizontal
OVERHANG_RIGHT = 35.0
CONES = ((8.0, 2.5), (22.0, 1.75))  # (x from the block's right edge, base radius)
CONE_H = 6.0
V_HOLE_D = 3.0       # vertical hole through the label block
H_HOLE_D = 4.0       # horizontal hole through pillar B
H_HOLE_Z = 4.5
BASE_MARGIN = 4.0    # base plate beyond the overhangs (x) and the tower (y)
LABEL_MAX_WIDTH = BLOCK_W - 4.0
SEGMENTS = 48
EPS = 0.05           # overlap between touching solids

PILLAR_X = BLOCK_W + BRIDGE
LEFT_RUN = FLOOR_H / math.tan(math.radians(OVERHANG_LEFT))
RIGHT_RUN = FLOOR_H / math.tan(math.radians(OVERHANG_RIGHT))
X_MIN = -LEFT_RUN - BASE_MARGIN
X_MAX = PILLAR_X + PILLAR_W + RIGHT_RUN + BASE_MARGIN
# Everything is shifted so the base plate is centred on the origin.
CX = (X_MIN + X_MAX) / 2
CY = DEPTH / 2


class Mesh:
    def __init__(self, verts=None, tris=None):
        self.verts = verts or []   # [(x, y, z)]
        self.tris = tris or []     # [(a, b, c)], counter-clockwise seen from outside

    @classmethod
    def unpack(cls, d):
        v = struct.unpack("<%df" % (3 * d["vcount"]), base64.b64decode(d["vertices"]))
        t = struct.unpack("<%dI" % (3 * d["tcount"]), base64.b64decode(d["triangles"]))
        return cls([v[i:i + 3] for i in range(0, len(v), 3)], [t[i:i + 3] for i in range(0, len(t), 3)])

    def moved(self, dx=0.0, dy=0.0, dz=0.0, sx=1.0, sz=1.0):
        return Mesh([(x * sx + dx, y + dy, z * sz + dz) for x, y, z in self.verts], list(self.tris))

    def extend(self, *others):
        for other in others:
            n = len(self.verts)
            self.verts.extend(other.verts)
            self.tris.extend((a + n, b + n, c + n) for a, b, c in other.tris)
        return self

    def volume(self):
        v = 0.0
        for a, b, c in self.tris:
            (x1, y1, z1), (x2, y2, z2), (x3, y3, z3) = self.verts[a], self.verts[b], self.verts[c]
            v += (x1 * (y2 * z3 - y3 * z2) - y1 * (x2 * z3 - x3 * z2) + z1 * (x2 * y3 - x3 * y2)) / 6
        return v


# ------------------------------------------------------------- primitives
def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _newell(pts):
    n = [0.0, 0.0, 0.0]
    for i, p in enumerate(pts):
        q = pts[(i + 1) % len(pts)]
        n[0] += (p[1] - q[1]) * (p[2] + q[2])
        n[1] += (p[2] - q[2]) * (p[0] + q[0])
        n[2] += (p[0] - q[0]) * (p[1] + q[1])
    return n


def prism(base, vec):
    """Extrude a planar convex polygon (3D points) along vec."""
    base = list(base)
    if _dot(_newell(base), vec) < 0:
        base.reverse()  # make the base counter-clockwise about vec
    m = len(base)
    top = [(p[0] + vec[0], p[1] + vec[1], p[2] + vec[2]) for p in base]
    tris = [(0, i + 1, i) for i in range(1, m - 1)]                 # bottom faces -vec
    tris += [(m, m + i, m + i + 1) for i in range(1, m - 1)]        # top faces +vec
    for i in range(m):
        j = (i + 1) % m
        tris += [(i, j, m + j), (i, m + j, m + i)]
    return Mesh(base + top, tris)


def box(x0, y0, z0, x1, y1, z1):
    return prism([(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0)], (0, 0, z1 - z0))


def xz_prism(points_xz, y0, y1):
    """A polygon drawn in the front (x, z) plane, extruded through the depth."""
    return prism([(x, y0, z) for x, z in points_xz], (0, y1 - y0, 0))


def circle(cx, cy, r, z=0.0, n=SEGMENTS):
    return [(cx + r * math.cos(2 * math.pi * i / n), cy + r * math.sin(2 * math.pi * i / n), z) for i in range(n)]


def cylinder_z(cx, cy, r, z0, z1):
    return prism(circle(cx, cy, r, z0), (0, 0, z1 - z0))


def cylinder_y(cx, cz, r, y0, y1, n=SEGMENTS):
    pts = [(cx + r * math.cos(2 * math.pi * i / n), y0, cz + r * math.sin(2 * math.pi * i / n)) for i in range(n)]
    return prism(pts, (0, y1 - y0, 0))


def cone(cx, cy, r, z0, h, n=SEGMENTS):
    base = circle(cx, cy, r, z0, n)
    apex = len(base)
    tris = [(0, i + 1, i) for i in range(1, n - 1)]
    tris += [(i, (i + 1) % n, apex) for i in range(n)]
    return Mesh(base + [(cx, cy, z0 + h)], tris)


# ------------------------------------------------------------------ tower
def floor_mesh(z0):
    """Positive solids of one floor whose bottom is at z0 (uncentred x)."""
    h, d = FLOOR_H, DEPTH
    m = Mesh()
    m.extend(
        box(0, 0, z0, BLOCK_W, d, z0 + h),                                        # label block A
        xz_prism([(EPS, z0), (EPS, z0 + h), (-LEFT_RUN, z0 + h)], 0, d),          # 45° overhang
        box(PILLAR_X, 0, z0, PILLAR_X + PILLAR_W, d, z0 + h),                     # pillar B
        xz_prism([(PILLAR_X + PILLAR_W - EPS, z0), (PILLAR_X + PILLAR_W + RIGHT_RUN, z0 + h),
                  (PILLAR_X + PILLAR_W - EPS, z0 + h)], 0, d),                    # 35° overhang
        box(BLOCK_W - 1, 0, z0 + h - BRIDGE_T, PILLAR_X + 1, d, z0 + h),          # bridge
    )
    for x, r in CONES:
        m.extend(cone(BLOCK_W + x, d / 2, r, z0, CONE_H))
    return m


def base_mesh():
    return box(X_MIN, -BASE_MARGIN, 0, X_MAX, DEPTH + BASE_MARGIN, BASE_H)


def holes_mesh(n_floors):
    top = BASE_H + n_floors * FLOOR_H
    m = cylinder_z(BLOCK_W / 2, DEPTH * 0.65, V_HOLE_D / 2, BASE_H, top + 1)
    for k in range(n_floors):
        z0 = BASE_H + k * FLOOR_H
        m.extend(cylinder_y(PILLAR_X + PILLAR_W / 2, z0 + H_HOLE_Z, H_HOLE_D / 2, -1, DEPTH + 1))
    return m


_digits = None


def digits():
    global _digits
    if _digits is None:
        with open(DIGITS) as f:
            _digits = json.load(f)
        _digits["mesh"] = {ch: Mesh.unpack(g) for ch, g in _digits["glyphs"].items()}
    return _digits


def text_mesh(text, cx, cz, max_width, height=None):
    """Engraving prisms for `text` on the front face (y=0), centred on (cx, cz).

    `height` scales the ink height (default: the glyphs' own 7 mm); the text
    is squeezed horizontally if it would be wider than `max_width`.
    """
    font = digits()
    glyphs = font["glyphs"]
    scale = height / font["digit_height"] if height else 1.0
    pens, pen = [], 0.0
    for ch in text:
        pens.append(pen)
        pen += glyphs[ch].get("advance", font["advance"])
    left = glyphs[text[0]]["xmin"]
    right = pens[-1] + glyphs[text[-1]]["xmax"]
    width = (right - left) * scale
    sx = scale * (min(1.0, max_width / width) if width > 0 else 1.0)
    zmin = min(glyphs[c]["zmin"] for c in text)
    zmax = max(glyphs[c]["zmax"] for c in text)
    dz = cz - (zmin + zmax) / 2 * scale
    x0 = cx - (left + right) / 2 * sx
    out = Mesh()
    for ch, p in zip(text, pens):
        out.extend(font["mesh"][ch].moved(dx=x0 + p * sx, dz=dz, sx=sx, sz=scale))
    return out


def raised_text_mesh(text, cx, cy, z0, max_width, height, raise_h=0.6):
    """Text standing `raise_h` mm up from a flat top surface at z0, readable from above.

    The glyph prisms are made for a front face (depth along +y); this lays them
    flat: their up direction becomes +y and their depth becomes height above z0.
    They dip 0.05 mm into the surface so the two solids overlap rather than touch.
    """
    font = digits()
    span = font["depth"] + font["overshoot"]
    k = (raise_h + 0.05) / span
    flat = text_mesh(text, 0.0, 0.0, max_width, height)
    flat.verts = [(cx + x, cy + z, z0 - 0.05 + (font["depth"] - y) * k) for x, y, z in flat.verts]
    return flat


def label_mesh(text, z0):
    """Engraving prisms for `text`, centred on the front of the label block of the floor at z0."""
    return text_mesh(text, BLOCK_W / 2, z0 + FLOOR_H / 2 - BRIDGE_T / 2, LABEL_MAX_WIDTH)


def tower_height(n_floors):
    return BASE_H + FLOOR_H * n_floors


def floor_bottom(k):
    return BASE_H + k * FLOOR_H


def build_parts(temps):
    """[(name, subtype, Mesh)] for the whole tower, base at z=0, centred on x/y."""
    def centred(m):
        return m.moved(dx=-CX, dy=-CY)

    floors = [floor_mesh(floor_bottom(k)) for k in range(len(temps))]
    floors[0].extend(base_mesh())
    parts = [("Base + floor 1 (%d°C)" % temps[0], "normal_part", centred(floors[0]))]
    parts += [("Floor %d (%d°C)" % (k + 1, t), "normal_part", centred(floors[k]))
              for k, t in enumerate(temps) if k]
    parts += [("Label %d" % t, "negative_part", centred(label_mesh(str(t), floor_bottom(k))))
              for k, t in enumerate(temps)]
    parts.append(("Holes", "negative_part", centred(holes_mesh(len(temps)))))
    return parts
