r"""Build a flow rate test whose top surfaces push extra plastic into a ring.

Each patch is a round pad with its own flow ratio and a label tab in front.
Its top is an Archimedean spiral printed in two passes, the way OrcaSlicer's
flow test does it:

        _____         1. the outer half, from the edge in to the ring
      /  ___  \       2. hop to the centre
     |  ( o )  |      3. the inner half, from the centre out to the ring
      \_______/
     |__0.980__|      raised flow ratio

Both passes push their extra plastic ahead of the nozzle, so it collects where
they meet: too much flow shows as a raised ring, too little as a groove or gaps
there. Ending at a ring rather than at the centre spreads the excess out, so it
doesn't pile up into a blob even at low flow. The ring sits at 1/sqrt(2) of
the radius, which puts half the surface on each side.

Round pads give a round spiral: with no corners for the nozzle to slow into and
speed out of, the result doesn't depend on pressure advance being tuned yet.

QIDI Studio prints the spiral in one go (edge to centre, or the other way), so
the test is sliced with QIDI Studio's command-line slicer and each pad's spiral
is split and reordered in the G-code.
"""
import math
import re

import project3mf
import qidi_profiles as qp
import tower_geometry as geo
from sliced_gcode import box_start_lines, END_MARK, START_MARK, body_extrusion, num, save_gcode, slice_to_gcode

PAD = 30.0          # pad diameter
PAD_SEGMENTS = 96   # sides of the pad's circle (3.75 degrees each)
PAD_H = 1.6         # pad thickness
TAB_D = 9.0         # label tab depth, in front of the pad
TAB_TUCK = 3.0      # how far the tab reaches under the pad
TAB_H = 0.8         # label tab thickness
TEXT_H = 5.0        # label text height
TEXT_RAISE = 0.6    # label text height above the tab
GAP = 5.0           # space between samples

TOP_MARK = "; FEATURE: Top surface"
SPLIT = 1 / math.sqrt(2)   # meeting ring, as a share of the pad radius: half the surface on each side


# ------------------------------------------------------------------ settings
def offsets(start, end, step):
    """Flow changes in percent, e.g. (-20, 20, 5) -> -20, -15, ... 20."""
    start, end, step = float(start), float(end), abs(float(step))
    if step <= 0:
        raise ValueError("Step must be more than 0 %")
    n = (end - start) / step
    if abs(n - round(n)) > 1e-6:
        raise ValueError("%g→%g %% is not a whole number of %g %% steps" % (start, end, step))
    sign = 1 if end >= start else -1
    values = [round(start + sign * i * step, 4) for i in range(abs(int(round(n))) + 1)]
    if any(v <= -100 for v in values):
        raise ValueError("Flow can't be reduced by 100 % or more")
    return values


def preset_flow(store, filament):
    return qp.as_float(store.resolve("filament", filament).get("filament_flow_ratio"), 1.0)


def flow_label(v):
    """0.98 -> "0.98", 0.931 -> "0.931", 1.0 -> "1.00"."""
    s = "%.3f" % v
    return s[:-1] if s.endswith("0") else s


def new_flow(base, offset):
    """The flow ratio a patch printed with: current flow × (1 + its change in %)."""
    if offset <= -100:
        raise ValueError("Flow can't be reduced by 100 % or more")
    return round(base * (1 + offset / 100.0), 3)


def ratios(base, offs):
    """The filament flow ratio each patch prints with."""
    return [new_flow(base, o) for o in offs]


# ------------------------------------------------------------------ geometry
def sample_parts(label):
    """[(name, subtype, Mesh)] for one sample, centred on x/y, base at z=0."""
    d = PAD + TAB_D
    y0 = -d / 2
    # Round pads give round rings: no corners for the nozzle to slow into, so the
    # result doesn't depend on pressure advance being tuned yet.
    pad = geo.cylinder_z(0.0, y0 + TAB_D + PAD / 2, PAD / 2, 0, PAD_H, n=PAD_SEGMENTS)
    # The tab tucks under the disc so the two print as one piece.
    tab = geo.box(-PAD / 2, y0, 0, PAD / 2, y0 + TAB_D + TAB_TUCK, TAB_H)
    text = geo.raised_text_mesh(label, 0.0, y0 + TAB_D / 2, TAB_H, PAD - 4, TEXT_H, TEXT_RAISE)
    return [("Pad", "normal_part", pad), ("Label tab", "normal_part", tab.extend(text))]


def layout(n):
    """(columns, rows) for n samples, roughly square, wider than deep."""
    cols = int(math.ceil(math.sqrt(n)))
    return cols, int(math.ceil(n / float(cols)))


def footprint(n):
    cols, rows = layout(n)
    return cols * PAD + (cols - 1) * GAP, rows * (PAD + TAB_D) + (rows - 1) * GAP


def build_objects(base, offs):
    """One object per patch, left to right then front to back... in reading order from the back-left."""
    cols, rows = layout(len(offs))
    width, depth = footprint(len(offs))
    objects = []
    for i, (o, ratio) in enumerate(zip(offs, ratios(base, offs))):
        col, row = i % cols, i // cols
        dx = -width / 2 + PAD / 2 + col * (PAD + GAP)
        dy = depth / 2 - (PAD + TAB_D) / 2 - row * (PAD + TAB_D + GAP)
        label = flow_label(ratio)
        objects.append({
            "name": "Flow %s (%+g%%)" % (label, o),
            "parts": sample_parts(label),
            "offset": (dx, dy),
            "settings": {
                "print_flow_ratio": num(1 + o / 100.0),
                "top_surface_pattern": "archimedeanchords",
                "top_shell_layers": "5",
                "bottom_shell_layers": "3",
                "ironing_type": "no ironing",
            },
        })
    return objects


# ---------------------------------------------------------- G-code rewriting
_MOVE = re.compile(r"^G[01] ")


def _words(line):
    return {w[0]: w[1:] for w in line.partition(";")[0].split()[1:] if w}


def _ends_block(line):
    if line.startswith(("; FEATURE:", "; CHANGE_LAYER", "; WIPE_START", "; Z_HEIGHT", "; LAYER_HEIGHT",
                        "EXCLUDE_OBJECT_END", "EXCLUDE_OBJECT_START", "; MACHINE_END_GCODE_START")):
        return True
    if _MOVE.match(line):
        w = _words(line)
        return "Z" in w or float(w.get("E", 0) or 0) < 0
    return False


def _fmt_xy(v):
    return ("%.3f" % v).rstrip("0").rstrip(".")


def _config(gcode, key, default):
    """A value from the CONFIG_BLOCK QIDI Studio writes at the end of its G-code (first extruder)."""
    m = re.search(r"^; %s = (.*)$" % re.escape(key), gcode, re.M)
    if not m:
        return default
    value = m.group(1).split(",")[0].strip().strip('"')
    return default if value in ("", "nil") else value


class _Seg:
    """One move: from a to b, extruding e, with the feed rate, acceleration and line width it used."""
    __slots__ = ("a", "b", "e", "f", "accel", "width")

    def __init__(self, a, b, e, f, accel, width):
        self.a, self.b, self.e, self.f, self.accel, self.width = a, b, e, f, accel, width

    def reversed(self):
        return _Seg(self.b, self.a, self.e, self.f, self.accel, self.width)

    def length(self):
        return math.hypot(self.b[0] - self.a[0], self.b[1] - self.a[1])


def _parse_block(block, start, f, accel):
    """Moves of a top-surface block. Returns (segments, final feed rate, final acceleration line)."""
    segs, pos, width = [], start, None
    for line in block:
        if line.startswith("; LINE_WIDTH:"):
            width = line
        elif line.startswith("M204 "):
            accel = line
        elif line.startswith("M73 ") or not line.strip() or line.startswith(";"):
            continue
        elif _MOVE.match(line):
            w = _words(line)
            if "F" in w:
                f = w["F"]
            if "X" not in w and "Y" not in w:
                continue
            to = (float(w.get("X", pos[0])), float(w.get("Y", pos[1])))
            segs.append(_Seg(pos, to, float(w.get("E", 0) or 0), f, accel, width))
            pos = to
        else:
            raise RuntimeError("Unexpected line in a top surface: %r" % line)
    return segs, f, accel


def _emit(segs, out, cur):
    """Write segments, re-stating line width, acceleration and feed rate whenever they change.
    `cur` is a dict of what's currently in effect; it's updated."""
    for s in segs:
        if s.width and s.width != cur.get("width"):
            out.append(s.width)
            cur["width"] = s.width
        if s.accel and s.accel != cur.get("accel"):
            out.append(s.accel)
            cur["accel"] = s.accel
        if s.f and s.f != cur.get("f"):
            out.append("G1 F" + s.f)
            cur["f"] = s.f
        if s.e > 0:
            out.append("G1 X%s Y%s E%s" % (_fmt_xy(s.b[0]), _fmt_xy(s.b[1]), num(s.e)))
        else:
            out.append("G1 X%s Y%s" % (_fmt_xy(s.b[0]), _fmt_xy(s.b[1])))


def _redirect_travel_in(out, target):
    """Point the slicer's (z-hopped) travel into the block at `target`. out[-1] is the FEATURE line."""
    last = len(out) - 2 if out and out[-1].startswith(TOP_MARK) else len(out) - 1
    for i in range(last, -1, -1):
        line = out[i]
        if _MOVE.match(line):
            w = _words(line)
            if "X" in w or "Y" in w:
                if float(w.get("E", 0) or 0) > 0:
                    out.insert(last + 1, "G1 X%s Y%s F12000" % (_fmt_xy(target[0]), _fmt_xy(target[1])))
                else:
                    words = [t for t in line.partition(";")[0].split() if t[0] not in "XY"]
                    out[i] = " ".join([words[0], "X" + _fmt_xy(target[0]), "Y" + _fmt_xy(target[1])] + words[1:])
                return
        elif line.startswith(("G2 ", "G3 ")) and ("X" in _words(line) or "Y" in _words(line)):
            raise RuntimeError("Can't redirect an arc travel into a top surface")
        elif line.startswith(("; FEATURE:", "; CHANGE_LAYER")):
            break
    out.insert(last + 1, "G1 X%s Y%s F12000" % (_fmt_xy(target[0]), _fmt_xy(target[1])))


def _split_at_radius(seg, c, r):
    """Split a segment that crosses the circle of radius r around c. Returns (before, after)."""
    ax, ay = seg.a[0] - c[0], seg.a[1] - c[1]
    dx, dy = seg.b[0] - seg.a[0], seg.b[1] - seg.a[1]
    qa, qb, qc = dx * dx + dy * dy, 2 * (ax * dx + ay * dy), ax * ax + ay * ay - r * r
    disc = max(0.0, qb * qb - 4 * qa * qc)
    roots = [t for t in ((-qb - math.sqrt(disc)) / (2 * qa), (-qb + math.sqrt(disc)) / (2 * qa)) if 0 <= t <= 1]
    t = min(roots) if roots else 0.5
    p = (seg.a[0] + dx * t, seg.a[1] + dy * t)
    return (_Seg(seg.a, p, seg.e * t, seg.f, seg.accel, seg.width),
            _Seg(p, seg.b, seg.e * (1 - t), seg.f, seg.accel, seg.width))


def _spiral_plan(segs, min_size):
    """If segs are one unbroken pad-sized spiral, return (outer, inner, centre): outer runs from the
    edge in to the meeting ring, inner from the centre out to it. Otherwise None."""
    if not segs:
        return None
    xs = [p[0] for s in segs for p in (s.a, s.b) if s.e > 0]
    ys = [p[1] for s in segs for p in (s.a, s.b) if s.e > 0]
    if not xs or max(xs) - min(xs) < min_size:
        return None
    c = ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)

    def radius(p):
        return math.hypot(p[0] - c[0], p[1] - c[1])

    # The spiral is the longest unbroken extrusion. QIDI Studio sometimes prints a few
    # tiny clipped stubs at the very edge first; those are kept and printed first too.
    runs, cur = [], []
    for s in segs:
        if s.e > 0:
            cur.append(s)
        elif cur:
            runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    main = max(runs, key=lambda run: sum(s.length() for s in run))
    total = sum(s.length() for s in segs if s.e > 0)
    if sum(s.length() for s in main) < 0.95 * total:
        return None              # not one spiral (label tabs, digits)
    r_max = max(radius(p) for s in main for p in (s.a, s.b))
    r_split = SPLIT * r_max
    stubs = [s for s in segs if not any(s is m for m in main)]
    if any(s.e > 0 and min(radius(s.a), radius(s.b)) < r_split for s in stubs):
        return None
    while stubs and stubs[-1].e <= 0:
        stubs.pop()              # the travel into the spiral is re-made below
    if radius(main[0].a) < radius(main[-1].b):   # spiral runs outwards: make it run inwards
        main = [s.reversed() for s in reversed(main)]
    for k, s in enumerate(main):
        if radius(s.a) >= r_split > radius(s.b):
            before, after = _split_at_radius(s, c, r_split)
            outer = main[:k] + [before]
            if stubs:
                hop = _Seg(stubs[-1].b, outer[0].a, 0.0, "30000", stubs[-1].accel, stubs[-1].width)
                outer = stubs + [hop] + outer
            inner = [x.reversed() for x in reversed([after] + main[k + 1:])]
            return outer, inner, c
    return None


def _rewrite_wipe(lines, i, trail, end_before, out, state):
    """Rewrite the wipe that follows a rewritten block (lines[i] is just after "; WIPE_START").

    The nozzle now stops somewhere else, so the wipe runs back along `trail` (the
    segments printed last, in print order) for the original wipe's length."""
    j = i
    moves, others = [], []
    while j < len(lines) and not lines[j].startswith("; WIPE_END"):
        line = lines[j]
        w = _words(line) if _MOVE.match(line) else {}
        if ("X" in w or "Y" in w) and float(w.get("E", 0) or 0) < 0:
            moves.append((float(w.get("X", 0)), float(w.get("Y", 0)), float(w["E"])))
        else:
            others.append(line)
        j += 1
    if not moves:
        return i
    total_e = sum(m[2] for m in moves)
    length, px, py = 0.0, end_before[0], end_before[1]
    for x, y, _ in moves:
        length += math.hypot(x - px, y - py)
        px, py = x, y
    pts, walked = [], 0.0
    for s in reversed(trail):
        if s.e <= 0:
            break
        seg = s.length()
        if seg <= 0:
            continue
        if walked + seg >= length:
            t = (length - walked) / seg
            pts.append((s.b[0] + (s.a[0] - s.b[0]) * t, s.b[1] + (s.a[1] - s.b[1]) * t, length - walked))
            walked = length
            break
        pts.append((s.a[0], s.a[1], seg))
        walked += seg
    out.extend(others)
    if walked <= 1e-6:
        out.append("G1 E%s F1800" % num(total_e))   # nowhere to wipe: plain retraction
    else:
        for x, y, d in pts:
            out.append("G1 X%s Y%s E%s" % (_fmt_xy(x), _fmt_xy(y), num(total_e * d / walked)))
        state["pos"] = (pts[-1][0], pts[-1][1])
    return j


def split_spirals(gcode, min_size=PAD * 0.9):
    """Reorder each pad's Archimedean top surface the way Orca does it: the outer half from the
    edge inwards, then (after a retract and hop to the middle) the inner half from the centre
    outwards, so both meet at a ring and any extra plastic piles up there.

    Returns (new_gcode, number of surfaces reordered)."""
    retract = float(_config(gcode, "filament_retraction_length", None) or _config(gcode, "retraction_length", "0.8"))
    retract_f = float(_config(gcode, "retraction_speed", "30")) * 60
    deretract_f = float(_config(gcode, "deretraction_speed", "0")) * 60 or retract_f
    hop = float(_config(gcode, "z_hop", "0.4"))
    travel_f = min(float(_config(gcode, "travel_speed", "300")) * 60, 30000)

    lines = gcode.split("\n")
    out, count, in_body = [], 0, False
    state = {"pos": (None, None), "f": None, "accel": None, "z": 0.0}
    pending = None             # (trail, where the block used to end) for the wipe that follows
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith(START_MARK):
            in_body = True
        elif line.startswith(END_MARK):
            in_body = False
        elif line.startswith("; Z_HEIGHT:"):
            state["z"] = float(line.split(":", 1)[1])
        if in_body and line.startswith(TOP_MARK) and state["pos"][0] is not None:
            j = i + 1
            while j < len(lines) and not _ends_block(lines[j]):
                j += 1
            segs, f_end, accel_end = _parse_block(lines[i + 1:j], state["pos"], state["f"], state["accel"])
            plan = _spiral_plan(segs, min_size)
            if plan:
                outer, inner, _ = plan
                out.append(line)
                _redirect_travel_in(out, outer[0].a)
                cur = {"f": state["f"], "accel": state["accel"]}
                _emit(outer, out, cur)
                # Jump to the middle like a normal travel: retract, hop, move, lower, un-retract.
                z = state["z"]
                out += ["G1 E-%s F%s" % (num(retract), num(retract_f)),
                        "G1 Z%s F%s" % (num(z + hop), num(travel_f)),
                        "G1 X%s Y%s F%s" % (_fmt_xy(inner[0].a[0]), _fmt_xy(inner[0].a[1]), num(travel_f)),
                        "G1 Z%s" % num(z),
                        "G1 E%s F%s" % (num(retract), num(deretract_f)),
                        line]
                cur = {}
                _emit(inner, out, cur)
                # Leave the modal state as the original block did.
                if f_end:
                    out.append("G1 F" + f_end)
                if accel_end:
                    out.append(accel_end)
                state.update(f=f_end, accel=accel_end, pos=inner[-1].b)
                pending = (inner, segs[-1].b)
                count += 1
                i = j
                continue
        if in_body and pending and line.startswith("; WIPE_START"):
            out.append(line)
            i = _rewrite_wipe(lines, i + 1, pending[0], pending[1], out, state)
            pending = None
            continue
        if pending and (line.startswith(("; FEATURE:", "; CHANGE_LAYER")) or
                        (_MOVE.match(line) and float(_words(line).get("E", 0) or 0) > 0)):
            pending = None
        if _MOVE.match(line):
            w = _words(line)
            x = float(w["X"]) if "X" in w else state["pos"][0]
            y = float(w["Y"]) if "Y" in w else state["pos"][1]
            state["pos"] = (x, y)
            if "F" in w:
                state["f"] = w["F"]
        elif line.startswith("M204 "):
            state["accel"] = line
        out.append(line)
        i += 1
    return "\n".join(out), count


def top_surface_paths(gcode, min_size=PAD * 0.9):
    """For checking: for every pad-sized top-surface block, the (start, end) distance from its
    pad's centre. A reordered pad gives two blocks: (edge, ring) then (centre, ring)."""
    lines = gcode.split("\n")
    found, body, pos = [], False, (None, None)
    blocks = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith(START_MARK):
            body = True
        if body and line.startswith(TOP_MARK) and pos[0] is not None:
            j = i + 1
            while j < len(lines) and not _ends_block(lines[j]):
                j += 1
            segs, _, _ = _parse_block(lines[i + 1:j], pos, None, None)
            ext = [s for s in segs if s.e > 0]
            if ext:
                blocks.append(ext)
        if _MOVE.match(line):
            w = _words(line)
            pos = (float(w["X"]) if "X" in w else pos[0], float(w["Y"]) if "Y" in w else pos[1])
        i += 1
    # pads: group blocks by the centre of the larger of each consecutive pair
    k = 0
    while k < len(blocks):
        b = blocks[k]
        xs = [p[0] for s in b for p in (s.a, s.b)]
        ys = [p[1] for s in b for p in (s.a, s.b)]
        if max(xs) - min(xs) >= min_size:
            c = ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)
            r = lambda p: math.hypot(p[0] - c[0], p[1] - c[1])  # noqa: E731
            entry = [(r(b[0].a), r(b[-1].b))]
            if k + 1 < len(blocks):
                n = blocks[k + 1]
                if all(r(p) <= max(r(b[0].a), 0) for s in n for p in (s.a, s.b)) and r(n[0].a) < 1.0:
                    entry.append((r(n[0].a), r(n[-1].b)))
                    k += 1
            found.append(entry)
        k += 1
    return found


# ------------------------------------------------------------------ generate
def generate(path, offs, *, store, machine, process, filament, base_flow=None, nozzle_temp=None,
             progress=None, box_slot=None):
    """Slice the flow test and write it as ready-to-print G-code. Returns a dict describing it."""
    say = progress or (lambda msg: None)
    printer = project3mf.Printer(store, machine, process)
    m = store.resolve("machine", machine)
    x0, y0, x1, y1 = qp.bed_shape(m)
    width, depth = footprint(len(offs))
    if width > x1 - x0 - 10 or depth > y1 - y0 - 10:
        raise ValueError("%d patches need %g x %g mm, more than the bed has. Use fewer steps."
                         % (len(offs), width, depth))
    base = base_flow if base_flow else preset_flow(store, filament)
    overrides = {"filament_flow_ratio": [num(base)], "enable_arc_fitting": "0", "use_relative_e_distances": "1"}
    if nozzle_temp:
        overrides["nozzle_temperature"] = [str(int(nozzle_temp))]
    gcode, png = slice_to_gcode(dict(
        objects=build_objects(base, offs), center=printer.center, store=store, machine=machine,
        process=process, filament=filament, config_overrides=overrides,
        description="Flow rate test %+g%% to %+g%%" % (offs[0], offs[-1])), progress)
    say("Splitting the top spirals: outer half in, then centre out…")
    new_gcode, count = split_spirals(gcode)
    drift = abs(body_extrusion(new_gcode) - body_extrusion(gcode))
    if drift > 0.01:
        raise RuntimeError("Reordering the top surfaces changed the total extrusion by %.3f mm" % drift)
    # Every pad's top must now be two passes that meet at the ring: edge -> ring, then centre -> ring.
    pads = top_surface_paths(new_gcode)
    good = [p for p in pads if len(p) == 2 and p[0][0] > p[0][1] and p[1][0] < 1.0 and p[1][1] > p[1][0]]
    if len(good) < len(offs):
        raise RuntimeError("Couldn't split every patch's top surface (%d of %d)" % (len(good), len(offs)))
    labels = [flow_label(r) for r in ratios(base, offs)]
    header = "; FLOW_TEST: base flow ratio %s, patches (from the back left): %s\n" % (
        num(base), ", ".join("%s (%+g%%)" % (lab, o) for lab, o in zip(labels, offs)))
    save_gcode(path, new_gcode, png, header, box_start_lines(box_slot))
    return {"path": path, "patches": len(offs), "labels": labels, "base": base, "reordered": count,
            "temperature": nozzle_temp or qp.first(store.resolve("filament", filament).get("nozzle_temperature"))}
