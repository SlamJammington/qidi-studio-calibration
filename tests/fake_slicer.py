"""Stands in for QIDI Studio's command-line slicer in the tests.

Accepts `--slice 1 --export-3mf NAME --outputdir DIR PROJECT` and writes DIR/NAME:
the project plus a small synthetic Metadata/plate_1.gcode in QIDI Studio's
style (relative extrusion, a wipe and a retraction before every travel, and
an un-retraction afterwards), using the project's retraction length.
"""
import hashlib
import math
import json
import os
import sys
import zipfile


def gcode(retraction, height, layer=0.2, extra=0.0, nozzle="210", top=False):
    lines = ["; HEADER_BLOCK_START", "; total layer number: %d" % round(height / layer), "; HEADER_BLOCK_END",
             "M83", "G1 E5 F80 ; prime in the start G-code, must stay untouched",
             "; MACHINE_START_GCODE_END"]
    z = layer
    while z <= height + 1e-6:
        lines += ["; CHANGE_LAYER", "; Z_HEIGHT: %g" % round(z, 4), "; LAYER_HEIGHT: %g" % layer,
                  "G1 X10 Y10 E1.2", "G1 X20 Y10 E1.2",
                  "; WIPE_START", "G1 X18 Y10 E-%s" % round(retraction * 0.95, 5), "; WIPE_END",
                  "G1 E-%s F1800" % round(retraction * 0.05, 5),
                  "G1 X60 Y10 F30000",
                  "G1 E%s F1800" % round(retraction + extra, 5),
                  "G1 X70 Y10 E1.2"]
        z += layer
    if top:
        lines += spiral_top()
    lines += ["; MACHINE_END_GCODE_START", "G1 E-3 F1800 ; end G-code retraction, must stay untouched",
              "; CONFIG_BLOCK_START", "; nozzle_temperature = %s" % nozzle,
              "; retraction_length = %s" % retraction, "; CONFIG_BLOCK_END", ""]
    return "\n".join(lines)


def spiral_points(cx, cy, r_max, pitch=0.42, step_deg=10.0):
    """Points of an Archimedean spiral from r_max in to the centre."""
    pts, theta = [], 0.0
    turns = r_max / pitch
    total = turns * 360.0
    while theta <= total + 1e-9:
        r = r_max * (1 - theta / total)
        pts.append((round(cx + r * math.cos(math.radians(theta)), 4), round(cy + r * math.sin(math.radians(theta)), 4)))
        theta += step_deg
    return pts


def spiral_top(cx=100.0, cy=100.0, z=1.6, r_max=14.4, outward=False, stubs=False):
    """An Archimedean top surface the way QIDI Studio prints it: one unbroken spiral (edge to
    centre, or centre to edge), optionally after a few tiny clipped stubs at the edge, then a
    wipe back along the last line."""
    pts = spiral_points(cx, cy, r_max)
    if outward:
        pts.reverse()
    lines = []
    start = pts[0]
    if stubs:
        sx, sy = cx - r_max - 0.05, cy
        start = (sx, sy + 0.5)
    lines += ["G1 X%g Y%g Z%g F30000" % (start[0], start[1], z + 0.4), "G1 Z%g" % z, "G1 E.8 F1800",
              "; FEATURE: Top surface", "; LINE_WIDTH: 0.42", "G1 F9000", "M204 S2000"]
    if stubs:
        lines += ["G1 X%g Y%g E.01" % (sx, sy + 0.9), "G1 X%g Y%g F30000" % (sx, sy - 0.5), "G1 F9000",
                  "G1 X%g Y%g E.01" % (sx, sy - 0.9), "G1 X%g Y%g F30000" % pts[0], "G1 F9000"]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        lines.append("G1 X%g Y%g E%s" % (x1, y1, round(math.hypot(x1 - x0, y1 - y0) * 0.033, 5)))
    (xa, ya), (xb, yb) = pts[-1], pts[-2]
    lines += ["M204 S10000", "; WIPE_START", "G1 F9000",
              "G1 X%g Y%g E-.76" % (xb, yb), "; WIPE_END", "G1 E-.04 F1800",
              "G1 X%g Y%g Z%g F30000" % (cx + 20, cy, z + 0.4)]
    return lines


def tiny_png(w=4, h=3):
    """A small grey PNG, standing in for the plate picture QIDI Studio renders."""
    import struct
    import zlib

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    raw = b"".join(b"\x00" + b"\x80" * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def main(argv):
    out_name = argv[argv.index("--export-3mf") + 1]
    out_dir = argv[argv.index("--outputdir") + 1]
    project = argv[-1]
    with zipfile.ZipFile(project) as z:
        cfg = json.loads(z.read("Metadata/project_settings.config"))
        entries = {n: z.read(n) for n in z.namelist()}
    height = float(os.environ.get("FAKE_SLICER_HEIGHT", "11"))
    extra = float(os.environ.get("FAKE_SLICER_EXTRA", "0"))
    retraction = float(cfg.get("filament_retraction_length", ["0.8"])[0]) if cfg.get(
        "filament_retraction_length", ["nil"])[0] != "nil" else 0.8
    code = gcode(retraction, height, extra=extra, nozzle=cfg.get("nozzle_temperature", ["210"])[0],
                 top=os.environ.get("FAKE_SLICER_TOP") == "1").encode()
    entries["Metadata/plate_1.gcode"] = code
    entries["Metadata/plate_1.gcode.md5"] = hashlib.md5(code).hexdigest().upper().encode()
    entries["Metadata/plate_1.png"] = tiny_png()
    with zipfile.ZipFile(os.path.join(out_dir, out_name), "w") as z:
        for n, data in entries.items():
            z.writestr(n, data)
    with open(os.path.join(out_dir, "result.json"), "w") as f:
        json.dump({"return_code": 0, "error_string": "Success."}, f)


if __name__ == "__main__":
    main(sys.argv[1:])
