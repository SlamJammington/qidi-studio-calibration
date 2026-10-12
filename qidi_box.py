"""Read a QIDI Box's slots from the printer, and choose which slot a test prints from.

QIDI's Klipper firmware keeps the Box's slot contents in saved variables
(filament_slotN, color_slotN, vendor_slotN are numbers into the printer's
officiall_filas_list.cfg) and reports each slot's sensor live (runout_button is
1 when the slot is empty). Tool Tn loads whatever slot value_tn names, which is
how QIDI Studio picks a slot for a print: it sends
    SAVE_VARIABLE VARIABLE=value_t0 VALUE="'slot2'"
before the print starts. A test does the same at the top of its G-code.
"""
import configparser
import json
import os
import re
import urllib.parse
import urllib.request

import printer_upload as pu

SLOTS_PER_BOX = 4


class Slot:
    def __init__(self, index, material, kind, colour, vendor, loaded):
        self.index = index          # 0-based, as the firmware numbers them ("slot0")
        self.material = material    # e.g. "PLA Basic"
        self.kind = kind            # e.g. "PLA"
        self.colour = colour        # "#RRGGBB" or ""
        self.vendor = vendor        # e.g. "Generic"
        self.loaded = loaded

    @property
    def number(self):
        return self.index + 1       # what the Box's screen and people call it

    def describe(self):
        if not self.loaded:
            return "Slot %d: empty" % self.number
        name = " ".join(x for x in (self.vendor, self.material) if x and x != "Generic") or self.material
        return "Slot %d: %s%s" % (self.number, name or "unknown filament",
                                  " (Generic)" if self.vendor == "Generic" else "")

    def __repr__(self):
        return "Slot(%d, %r, %r, loaded=%s)" % (self.number, self.material, self.colour, self.loaded)


def _get(base, path, timeout):
    with urllib.request.urlopen(base + path, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def parse_filament_list(text):
    """The printer's officiall_filas_list.cfg: ({n: (name, type)}, {n: "#colour"}, {n: vendor})."""
    cfg = configparser.ConfigParser(interpolation=None, strict=False)
    cfg.read_string(text)
    materials = {}
    for sec in cfg.sections():
        m = re.match(r"fila(\d+)$", sec)
        if m:
            materials[int(m.group(1))] = (cfg[sec].get("filament", "").strip(), cfg[sec].get("type", "").strip())
    colours = {int(k): v.strip() for k, v in cfg["colordict"].items()} if cfg.has_section("colordict") else {}
    vendors = {int(k): v.strip() for k, v in cfg["vendor_list"].items()} if cfg.has_section("vendor_list") else {}
    return materials, colours, vendors


def local_filament_list():
    """QIDI Studio's own copy of the list, used if the printer's can't be read."""
    import studio
    cmd = studio.studio_command()
    if not cmd:
        return None
    exe = cmd[-1] if cmd[0] == "open" else cmd[0]
    folder = exe if os.path.isdir(exe) else os.path.dirname(exe)
    for path in (os.path.join(folder, "resources", "profiles", "officiall_filas_list.cfg"),
                 os.path.join(folder, "Contents", "Resources", "profiles", "officiall_filas_list.cfg")):
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                return f.read()
    return None


def read_box(printer, timeout=5):
    """The Box slots of a QIDI Studio network printer, or None if it has no Box.

    Raises OSError/ValueError if the printer can't be reached or doesn't answer like Klipper."""
    base = pu.base_url(printer)
    objects = json.loads(_get(base, "/printer/objects/list", timeout))["result"]["objects"]
    steppers = sorted((o for o in objects if o.startswith("box_stepper slot")),
                      key=lambda o: int(re.sub(r"\D", "", o) or 0))
    if not steppers:
        return None
    query = "&".join(urllib.parse.quote(o) for o in steppers + ["save_variables"])
    status = json.loads(_get(base, "/printer/objects/query?" + query, timeout))["result"]["status"]
    saved = status.get("save_variables", {}).get("variables", {})
    try:
        table = _get(base, "/server/files/config/officiall_filas_list.cfg", timeout)
    except OSError:
        table = local_filament_list() or ""
    materials, colours, vendors = parse_filament_list(table)
    slots = []
    for name in steppers:
        index = int(re.sub(r"\D", "", name))
        live = status.get(name, {})
        if "runout_button" in live:
            loaded = live["runout_button"] == 0          # the sensor reads 1 when the slot is empty
        else:
            loaded = saved.get("slot%d" % index) == 1
        material, kind = materials.get(saved.get("filament_slot%d" % index), ("", ""))
        slots.append(Slot(index, material, kind, colours.get(saved.get("color_slot%d" % index), ""),
                          vendors.get(saved.get("vendor_slot%d" % index), ""), loaded))
    return slots


def default_slot(slots, filament_type=""):
    """A sensible slot to start with: loaded, and holding the same kind of filament if possible."""
    loaded = [s for s in slots if s.loaded]
    for s in loaded:
        if filament_type and s.kind.upper() == filament_type.upper():
            return s
    return loaded[0] if loaded else None


def select_slot_gcode(index, tool=0):
    """The line QIDI Studio sends to make tool `tool` load from Box slot `index` (0-based)."""
    return "SAVE_VARIABLE VARIABLE=value_t%d VALUE=\"'slot%d'\"" % (tool, index)
