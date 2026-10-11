r"""Build a retraction test for QIDI Studio.

Two towers stand apart on a base plate, so every layer ends with a long travel
that needs a retraction. The towers are split into bands, each printed with a
different retraction length, engraved on the left tower.

      _____                                     _____
     | 1.0 |                                   |     |
     | 0.8 |  <-- 40 mm travel every layer --> |     |
     | 0.6 |                                   |     |
  ___|_0.4_|___________________________________|_____|___

QIDI Studio can't vary retraction by height, so the test is sliced with QIDI
Studio's own command-line slicer using a reference retraction length, and
every retraction (and the matching un-retraction) in the G-code is then scaled
to the band's length. The result is ready-to-print G-code: QIDI Studio can
preview it, and printer_upload.py sends it to the printer.
"""

import project3mf
import qidi_profiles as qp
import tower_geometry as geo
from sliced_gcode import END_MARK, START_MARK, body_extrusion, num as _num, save_gcode, slice_to_gcode  # noqa: F401

TOWER_W = 14.0       # tower width (x); the band labels go on the left tower's front
TOWER_D = 10.0       # tower depth (y)
GAP = 40.0           # travel distance between the towers
BASE_H = 1.0
BASE_MARGIN = 3.0
LABEL_H = 3.5        # engraved label height on bands of 5 mm or more



# ------------------------------------------------------------------ settings
def lengths(start, end, step):
    """Retraction lengths from the bottom band up, e.g. (0, 1, 0.2) -> 0, 0.2, ... 1.0."""
    start, end, step = float(start), float(end), abs(float(step))
    if step <= 0:
        raise ValueError("Step must be more than 0 mm")
    if start < 0 or end < 0:
        raise ValueError("Retraction lengths can't be negative")
    n = (end - start) / step
    if abs(n - round(n)) > 1e-6:
        raise ValueError(f"{fmt(start)}→{fmt(end)} mm is not a whole number of {fmt(step)} mm steps")
    sign = 1 if end >= start else -1
    return [round(start + sign * i * step, 4) for i in range(abs(int(round(n))) + 1)]


def fmt(v):
    """0 -> "0.0", 0.25 -> "0.25", 1.2 -> "1.2"."""
    s = ("%.2f" % v).rstrip("0")
    return s + "0" if s.endswith(".") else s


def preset_retraction(store, machine, filament):
    """The retraction length the presets would use (filament override first)."""
    f = store.resolve("filament", filament)
    m = store.resolve("machine", machine)
    override = qp.first(f.get("filament_retraction_length"), "nil")
    if override not in (None, "", "nil"):
        return qp.as_float(override, 0.0)
    return qp.as_float(m.get("retraction_length"), 0.0)


def defaults(store, machine):
    """(start, end, step): 0-2 mm for direct drive extruders, 0-6 mm for Bowden."""
    m = store.resolve("machine", machine)
    kind = " ".join(str(x) for x in (m.get("extruder_type") or []) + (m.get("printer_extruder_variant") or []))
    if "bowden" in kind.lower():
        return 0.0, 6.0, 0.5
    return 0.0, 2.0, 0.2


# ------------------------------------------------------------------ geometry
def test_height(n_bands, band_h):
    return BASE_H + n_bands * band_h


def build_parts(values, band_h):
    """[(name, subtype, Mesh)] for the test, base at z=0, centred on x/y."""
    top = test_height(len(values), band_h)
    width = 2 * TOWER_W + GAP
    cx, cy = width / 2, TOWER_D / 2
    left = geo.box(0, 0, BASE_H, TOWER_W, TOWER_D, top)
    right = geo.box(TOWER_W + GAP, 0, BASE_H, width, TOWER_D, top)
    base = geo.box(-BASE_MARGIN, -BASE_MARGIN, 0, width + BASE_MARGIN, TOWER_D + BASE_MARGIN, BASE_H)
    label_h = min(LABEL_H, band_h * 0.7)
    labels = geo.Mesh()
    for k, v in enumerate(values):
        labels.extend(geo.text_mesh(fmt(v), TOWER_W / 2, BASE_H + (k + 0.5) * band_h, TOWER_W - 3, label_h))

    def centred(m):
        return m.moved(dx=-cx, dy=-cy)

    return [("Base", "normal_part", centred(base)),
            ("Left tower", "normal_part", centred(left)),
            ("Right tower", "normal_part", centred(right)),
            ("Labels", "negative_part", centred(labels))]


# ---------------------------------------------------------- G-code rewriting
def rewrite_retractions(gcode, values, band_h, reference):
    """Scale each retraction to its band's length. Returns (new_gcode, retractions_per_band).

    Expects relative extrusion (M83), as QIDI Studio writes it. A retraction is
    any move with negative E (including wipes); the next extrude-only move with
    positive E undoes it, keeping any "extra length on restart" unchanged.
    """
    out = []
    counts = [0] * len(values)
    in_body = False
    band = 0
    marked = -1
    z = 0.0
    pulled = pulled_new = 0.0   # how far the filament is retracted: as sliced, and after rewriting
    for line in gcode.split("\n"):
        if line.startswith(START_MARK):
            in_body = True
        elif line.startswith(END_MARK):
            in_body = False
        if not in_body:
            out.append(line)
            continue
        if line.startswith("; Z_HEIGHT:"):
            z = float(line.split(":", 1)[1])
        elif line.startswith("; LAYER_HEIGHT:"):
            # Layers are sliced through their middle; the base counts as the first band.
            mid = z - float(line.split(":", 1)[1]) / 2
            band = min(len(values) - 1, max(0, int((mid - BASE_H) // band_h)))
            out.append(line)
            if mid > BASE_H and band != marked:
                out.append("; RETRACTION_TEST band %d: %s mm" % (band + 1, fmt(values[band])))
                marked = band
            continue
        if line[:3] in ("G0 ", "G1 ", "G2 ", "G3 "):
            code, sep, comment = line.partition(";")
            words = code.split()
            e_at = next((i for i, w in enumerate(words) if w[0] == "E"), None)
            if e_at is not None:
                e = float(words[e_at][1:])
                if e < 0:
                    new = float(_num(e * values[band] / reference))  # what gets written
                    pulled += -e
                    pulled_new += -new
                    counts[band] += 1
                    words[e_at] = "E" + _num(new)
                    line = " ".join(words) + (" " + sep + comment if sep else "")
                elif e > 0 and pulled > 0 and all(w[0] in "EF" for w in words[1:]):
                    new = float(_num(pulled_new + (e - pulled)))  # keeps "extra length on restart"
                    pulled = pulled_new = 0.0
                    words[e_at] = "E" + _num(new)
                    line = " ".join(words) + (" " + sep + comment if sep else "")
        out.append(line)
    return "\n".join(out), counts


# ------------------------------------------------------------------ slicing
def generate(path, values, *, store, machine, process, filament, band_h=5.0, nozzle_temp=None, progress=None):
    """Slice the retraction test and write it as ready-to-print G-code.

    Returns a dict describing what was written.
    """
    say = progress or (lambda msg: None)
    printer = project3mf.Printer(store, machine, process)
    height = test_height(len(values), band_h)
    printer.check_height(height, "The retraction test")
    if band_h < 2:
        raise ValueError("Bands must be at least 2 mm tall")

    m = store.resolve("machine", machine)
    f = store.resolve("filament", filament)
    reference = max(values) or 1.0
    n_lengths = len(m.get("retraction_length") or ["0"])
    overrides = {
        "retraction_length": [fmt(reference)] * n_lengths,
        "filament_retraction_length": [fmt(reference)],
        "use_relative_e_distances": "1",
    }
    if nozzle_temp:
        overrides["nozzle_temperature"] = [str(int(nozzle_temp))]

    gcode, png = slice_to_gcode(dict(
        parts=build_parts(values, band_h), center=printer.center, store=store, machine=machine,
        process=process, filament=filament, config_overrides=overrides,
        description="Retraction test %s-%s mm" % (fmt(values[0]), fmt(values[-1]))), progress)
    say("Adjusting retractions…")
    new_gcode, counts = rewrite_retractions(gcode, values, band_h, reference)
    drift = abs(body_extrusion(new_gcode) - body_extrusion(gcode))
    if drift > 0.01:
        raise RuntimeError("Retraction rewrite changed the total extrusion by %.3f mm" % drift)
    header = ("; RETRACTION_TEST: %s mm, bands of %g mm, from the bottom: %s\n"
              % (" -> ".join(fmt(v) for v in (values[0], values[-1])), band_h,
                 ", ".join(fmt(v) for v in values)))
    save_gcode(path, new_gcode, png, header)
    return {"path": path, "bands": len(values), "height": height, "retractions": counts,
            "temperature": nozzle_temp or qp.first(f.get("nozzle_temperature"))}
