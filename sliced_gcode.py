"""Slice a project with QIDI Studio's command-line slicer and save G-code from it.

Shared by the tests QIDI Studio can't make by itself (retraction, flow rate): they
are sliced headlessly, the G-code is adjusted, then saved as plain G-code with the
plate picture embedded as a thumbnail.
"""
import json
import os
import subprocess
import sys
import tempfile
import zipfile

import project3mf
import studio

START_MARK = "; MACHINE_START_GCODE_END"
END_MARK = "; MACHINE_END_GCODE_START"


def num(v):
    s = ("%.5f" % v).rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def body_extrusion(gcode):
    """Sum of E over the printed part of the G-code (relative extrusion)."""
    total, in_body = 0.0, False
    for line in gcode.split("\n"):
        if line.startswith(START_MARK):
            in_body = True
        elif line.startswith(END_MARK):
            in_body = False
        elif in_body and line[:3] in ("G0 ", "G1 ", "G2 ", "G3 "):
            for w in line.partition(";")[0].split():
                if w[0] == "E":
                    total += float(w[1:])
    return total


def slice_project(project, workdir, timeout=900):
    """Slice a project with QIDI Studio's CLI. Returns the path of the sliced .gcode.3mf."""
    cli = studio.cli_command()
    if not cli:
        raise FileNotFoundError("QIDI Studio was not found; it's needed to slice this test")
    out_name = "sliced.gcode.3mf"
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    proc = subprocess.run(cli + ["--slice", "1", "--export-3mf", out_name, "--outputdir", workdir, project],
                          capture_output=True, text=True, timeout=timeout, **kwargs)
    result = {}
    try:
        with open(os.path.join(workdir, "result.json"), encoding="utf-8") as f:
            result = json.load(f)
    except (OSError, ValueError):
        pass
    sliced = os.path.join(workdir, out_name)
    if result.get("return_code", proc.returncode) != 0 or not os.path.isfile(sliced):
        msg = result.get("error_string") or (proc.stderr or proc.stdout or "").strip()[-500:]
        raise RuntimeError("QIDI Studio couldn't slice the test: %s" % (msg or "unknown error"))
    return sliced


def thumbnail_block(png):
    """A PNG as a G-code thumbnail comment block, which Klipper/Moonraker shows on the printer."""
    import base64
    import struct
    if png[:8] != b"\x89PNG\r\n\x1a\n":
        return ""
    w, h = struct.unpack(">II", png[16:24])
    data = base64.b64encode(png).decode()
    lines = ["; THUMBNAIL_BLOCK_START", "; thumbnail begin %dx%d %d" % (w, h, len(data))]
    lines += ["; " + data[i:i + 78] for i in range(0, len(data), 78)]
    lines += ["; thumbnail end", "; THUMBNAIL_BLOCK_END", ""]
    return "\n".join(lines) + "\n"


def slice_to_gcode(project_kwargs, progress=None):
    """Write a project with project3mf.write_project(**project_kwargs), slice it, and
    return (gcode text, plate PNG bytes)."""
    say = progress or (lambda msg: None)
    with tempfile.TemporaryDirectory(prefix="qidi_calibration_") as work:
        project = os.path.join(work, "calibration.3mf")
        project3mf.write_project(project, **project_kwargs)
        say("Slicing with QIDI Studio…")
        sliced = slice_project(project, work)
        with zipfile.ZipFile(sliced) as z:
            gcode = z.read("Metadata/plate_1.gcode").decode("utf-8")
            png = z.read("Metadata/plate_1.png") if "Metadata/plate_1.png" in z.namelist() else b""
    if "M83" not in gcode:
        raise RuntimeError("The sliced G-code doesn't use relative extrusion (M83), so it can't be adjusted")
    return gcode, png


def save_gcode(path, gcode, png=b"", header=""):
    """Write G-code with the plate picture as a thumbnail and `header` lines after the header block."""
    marker = "; HEADER_BLOCK_END\n"
    gcode = gcode.replace(marker, marker + thumbnail_block(png) + header, 1)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(gcode)
    os.replace(tmp, path)
