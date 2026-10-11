r"""Build a flow rate test with concentric top surfaces printed from the outside in.

Each patch is a square pad with its own flow ratio and a label tab in front:

     _____________   _____________   _____________
    |  _________  | |  _________  | |  _________  |
    | |  _____  | | | |  _____  | | | |  _____  | |    concentric top surface,
    | | |  _  | | | | | |  _  | | | | | |  _  | | |    printed outside -> in
    | | |_|_| | | | | | |_|_| | | | | | |_|_| | | |
    | |_______| | | | |_______| | | | |_______| | |
    |_____________| |_____________| |_____________|
    |__ 0.931 ____| |__ 0.980 ____| |__ 1.029 ____|    raised flow ratio

Printing the rings from the edge inwards pushes any extra plastic towards the
middle, so over-extrusion shows up as a ridge or blob in the centre and
under-extrusion as gaps there. The patch with the cleanest centre wins.

QIDI Studio prints concentric top surfaces from the inside out and can't be
told otherwise, so the test is sliced with QIDI Studio's command-line slicer
and every top-surface path is reversed in the G-code.
"""
import math
import re

import project3mf
import qidi_profiles as qp
import tower_geometry as geo
from sliced_gcode import END_MARK, START_MARK, body_extrusion, num, save_gcode, slice_to_gcode

PAD = 30.0          # pad size (x and y)
PAD_H = 1.6         # pad thickness
TAB_D = 9.0         # label tab depth, in front of the pad
TAB_H = 0.8         # label tab thickness
TEXT_H = 5.0        # label text height
TEXT_RAISE = 0.6    # label text height above the tab
GAP = 5.0           # space between samples

TOP_MARK = "; FEATURE: Top surface"


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
    pad = geo.box(-PAD / 2, y0 + TAB_D, 0, PAD / 2, y0 + d, PAD_H)
    tab = geo.box(-PAD / 2, y0, 0, PAD / 2, y0 + TAB_D + 0.05, TAB_H)
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
                "top_surface_pattern": "concentric",
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


class _State:
    def __init__(self):
        self.x = self.y = None
        self.f = None
        self.accel = None


def _reverse_block(block, state, out):
    """Reverse one top-surface block. `block` excludes the FEATURE line; `out` holds the lines
    already written, whose last travel into the block gets redirected to the new start."""
    start = (state.x, state.y)
    segs = []                         # (from, to, e, f, accel, width)
    width = None
    pos, f, accel = start, state.f, state.accel
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
                continue              # "G1 F..." only
            to = (float(w.get("X", pos[0])), float(w.get("Y", pos[1])))
            segs.append((pos, to, float(w.get("E", 0) or 0), f, accel, width))
            pos = to
        else:
            raise RuntimeError("Unexpected line in a top surface: %r" % line)
    if not segs or start[0] is None:
        return None
    end = pos

    # Redirect the slicer's travel into the block so it lands on the new start point.
    # (out[-1] is this block's own FEATURE line.)
    last = len(out) - 2 if out and out[-1].startswith(TOP_MARK) else len(out) - 1
    for i in range(last, -1, -1):
        line = out[i]
        if _MOVE.match(line):
            w = _words(line)
            if "X" in w or "Y" in w:
                if float(w.get("E", 0) or 0) > 0:
                    out.append("G1 X%s Y%s F12000" % (_fmt_xy(end[0]), _fmt_xy(end[1])))
                else:
                    words = [t for t in line.partition(";")[0].split() if t[0] not in "XY"]
                    out[i] = " ".join([words[0], "X" + _fmt_xy(end[0]), "Y" + _fmt_xy(end[1])] + words[1:])
                break
        elif line.startswith(("G2 ", "G3 ")) and ("X" in _words(line) or "Y" in _words(line)):
            raise RuntimeError("Can't redirect an arc travel into a top surface")
        elif line.startswith(("; FEATURE:", "; CHANGE_LAYER")):
            out.append("G1 X%s Y%s F12000" % (_fmt_xy(end[0]), _fmt_xy(end[1])))
            break

    cur_w = cur_a = cur_f = None
    for frm, to, e, seg_f, seg_a, seg_w in reversed(segs):
        if seg_w and seg_w != cur_w:
            out.append(seg_w)
            cur_w = seg_w
        if seg_a and seg_a != cur_a:
            out.append(seg_a)
            cur_a = seg_a
        if seg_f and seg_f != cur_f:
            out.append("G1 F" + seg_f)
            cur_f = seg_f
        if e > 0:
            out.append("G1 X%s Y%s E%s" % (_fmt_xy(frm[0]), _fmt_xy(frm[1]), num(e)))
        else:
            out.append("G1 X%s Y%s" % (_fmt_xy(frm[0]), _fmt_xy(frm[1])))
    # Leave the modal state as the original block did.
    if f and f != cur_f:
        out.append("G1 F" + f)
    if accel and accel != cur_a:
        out.append(accel)
    state.f, state.accel = f, accel
    state.x, state.y = start
    # The wipe that follows should run back over the last printed line, which is now
    # the start of the original path.
    return [(frm, to, e) for frm, to, e, _, _, _ in segs], end


def _rewrite_wipe(lines, i, path, out, state):
    """Rewrite the wipe moves after a reversed block (lines[i] is "; WIPE_START")."""
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
    # Original wipe length, measured from where the block used to end.
    length, px, py = 0.0, state.end[0], state.end[1]
    for x, y, _ in moves:
        length += math.hypot(x - px, y - py)
        px, py = x, y
    # Walk the original path forwards from its start (now the last line printed),
    # for the same distance, stopping at the first travel.
    pts, walked = [], 0.0
    for frm, to, e in path:
        if e <= 0:
            break
        if frm == to:
            continue
        seg = math.hypot(to[0] - frm[0], to[1] - frm[1])
        if walked + seg >= length:
            t = (length - walked) / seg
            pts.append((frm[0] + (to[0] - frm[0]) * t, frm[1] + (to[1] - frm[1]) * t, length - walked))
            walked = length
            break
        pts.append((to[0], to[1], seg))
        walked += seg
    out.extend(others)
    if walked <= 1e-6:
        out.append("G1 E%s F1800" % num(total_e))   # nowhere to wipe: plain retraction
    else:
        for x, y, d in pts:
            out.append("G1 X%s Y%s E%s" % (_fmt_xy(x), _fmt_xy(y), num(total_e * d / walked)))
        state.x, state.y = pts[-1][0], pts[-1][1]
    return j


def reverse_top_surfaces(gcode):
    """Print every top surface in reverse. Returns (new_gcode, number of surfaces reversed)."""
    lines = gcode.split("\n")
    out = []
    state = _State()
    state.end = (0.0, 0.0)
    count = 0
    in_body = False
    pending_wipe = None
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith(START_MARK):
            in_body = True
        elif line.startswith(END_MARK):
            in_body = False
        if in_body and line.startswith(TOP_MARK):
            out.append(line)
            j = i + 1
            while j < len(lines) and not _ends_block(lines[j]):
                j += 1
            result = _reverse_block(lines[i + 1:j], state, out)
            if result:
                pending_wipe, state.end = result
                count += 1
            i = j
            continue
        if in_body and pending_wipe and line.startswith("; WIPE_START"):
            out.append(line)
            i = _rewrite_wipe(lines, i + 1, pending_wipe, out, state)
            pending_wipe = None
            continue
        if pending_wipe and (line.startswith(("; FEATURE:", "; CHANGE_LAYER")) or
                             (_MOVE.match(line) and float(_words(line).get("E", 0) or 0) > 0)):
            pending_wipe = None
        # track position and modal state
        if _MOVE.match(line):
            w = _words(line)
            if "X" in w:
                state.x = float(w["X"])
            if "Y" in w:
                state.y = float(w["Y"])
            if "F" in w:
                state.f = w["F"]
        elif line.startswith("M204 "):
            state.accel = line
        out.append(line)
        i += 1
    return "\n".join(out), count


def ring_order(gcode, center):
    """For checking: the distance from `center` of each top-surface extrusion run, in print order."""
    runs, cur, body, in_top = [], [], False, False
    x = y = 0.0
    for line in gcode.split("\n"):
        if line.startswith(START_MARK):
            body = True
        if not body:
            continue
        if line.startswith(TOP_MARK):
            in_top = True
            continue
        if in_top and _ends_block(line):
            in_top = False
        if _MOVE.match(line):
            w = _words(line)
            nx, ny = float(w.get("X", x)), float(w.get("Y", y))
            if in_top and float(w.get("E", 0) or 0) > 0:
                cur.append(max(abs(nx - center[0]), abs(ny - center[1])))
            elif cur:
                runs.append(max(cur))
                cur = []
            x, y = nx, ny
    if cur:
        runs.append(max(cur))
    return runs


# ------------------------------------------------------------------ generate
def generate(path, offs, *, store, machine, process, filament, base_flow=None, nozzle_temp=None,
             progress=None):
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
    say("Reversing the top surfaces…")
    new_gcode, count = reverse_top_surfaces(gcode)
    if count < len(offs):
        raise RuntimeError("Only %d of %d top surfaces could be reversed" % (count, len(offs)))
    drift = abs(body_extrusion(new_gcode) - body_extrusion(gcode))
    if drift > 0.01:
        raise RuntimeError("Reversing the top surfaces changed the total extrusion by %.3f mm" % drift)
    labels = [flow_label(r) for r in ratios(base, offs)]
    header = "; FLOW_TEST: base flow ratio %s, patches (from the back left): %s\n" % (
        num(base), ", ".join("%s (%+g%%)" % (lab, o) for lab, o in zip(labels, offs)))
    save_gcode(path, new_gcode, png, header)
    return {"path": path, "patches": len(offs), "labels": labels, "base": base, "reversed": count,
            "temperature": nozzle_temp or qp.first(store.resolve("filament", filament).get("nozzle_temperature"))}
