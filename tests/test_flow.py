import math
import os
import re
import sys
import tempfile
import unittest
import zipfile
import xml.etree.ElementTree as ET
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(ROOT, "tests")
sys.path.insert(0, ROOT)
sys.path.insert(0, TESTS)
import fake_slicer  # noqa: E402
import flow_test as ft  # noqa: E402
import project3mf  # noqa: E402
import qidi_profiles as qp  # noqa: E402
from sliced_gcode import body_extrusion  # noqa: E402

FIXTURE = os.path.join(TESTS, "fixtures", "QIDIStudio")
PRESETS = dict(machine="My Printer", process="0.20mm Standard @Test", filament="My PLA")
NS = {"m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"}


class SettingsTests(unittest.TestCase):
    def test_offsets_and_labels(self):
        self.assertEqual(ft.offsets(-20, 20, 5), [-20, -15, -10, -5, 0, 5, 10, 15, 20])
        self.assertEqual(ft.offsets(5, -5, 5), [5, 0, -5])
        with self.assertRaises(ValueError):
            ft.offsets(-20, 20, 7)
        self.assertEqual(ft.ratios(0.98, [-5, 0, 5]), [0.931, 0.98, 1.029])
        self.assertEqual([ft.flow_label(v) for v in (0.931, 0.98, 1.0)], ["0.931", "0.98", "1.00"])

    def test_calculator(self):
        self.assertEqual(ft.new_flow(0.98, 5), 1.029)
        self.assertEqual(ft.new_flow(1.0, -7.5), 0.925)
        with self.assertRaises(ValueError):
            ft.new_flow(1.0, -100)

    def test_preset_flow(self):
        self.assertEqual(ft.preset_flow(qp.PresetStore(FIXTURE), "My PLA"), 1.0)


def sample_gcode(**kw):
    head = ["; HEADER_BLOCK_START", "; HEADER_BLOCK_END", "M83", "; MACHINE_START_GCODE_END",
            "; CHANGE_LAYER", "; Z_HEIGHT: 1.6", "; LAYER_HEIGHT: 0.2", "G1 X90 Y90 F30000"]
    tail = ["G1 X150 Y150 E1", "; MACHINE_END_GCODE_START", "; CONFIG_BLOCK_START", "; retraction_length = 0.8",
            "; retraction_speed = 30", "; z_hop = 0.4", "; travel_speed = 500", "; CONFIG_BLOCK_END", ""]
    return "\n".join(head + fake_slicer.spiral_top(**kw) + tail)


def radius(p):
    return math.hypot(p[0] - 100, p[1] - 100)


class SplitTests(unittest.TestCase):
    def check_split(self, src):
        new, count = ft.split_spirals(src)
        self.assertEqual(count, 1)
        self.assertAlmostEqual(body_extrusion(new), body_extrusion(src), places=4)
        passes = ft.top_surface_paths(new)
        self.assertEqual(len(passes), 1)
        (o_start, o_end), (i_start, i_end) = passes[0]
        ring = ft.SPLIT * 14.4
        self.assertGreater(o_start, 14.0)                    # outer pass starts at the edge...
        self.assertAlmostEqual(o_end, ring, delta=0.15)      # ...and stops at the ring
        self.assertLess(i_start, 0.5)                        # inner pass starts at the centre...
        self.assertAlmostEqual(i_end, o_end, delta=0.01)     # ...and works out to the same point
        return new

    def test_edge_to_centre_spiral(self):
        self.check_split(sample_gcode())

    def test_centre_to_edge_spiral(self):
        self.check_split(sample_gcode(outward=True))

    def test_edge_stubs_print_first(self):
        new = self.check_split(sample_gcode(stubs=True))
        top = new.split(ft.TOP_MARK, 1)[1].split("\n")
        self.assertEqual(top[4], "G1 X85.55 Y100.9 E0.01")   # the stub, still first

    def test_monotonic_inside_each_pass(self):
        new = self.check_split(sample_gcode())
        outer, inner = [part.split("; WIPE_START")[0] for part in new.split(ft.TOP_MARK)[1:3]]
        def radii(text):
            return [radius((float(x), float(y))) for x, y in re.findall(r"G1 X([\d.]+) Y([\d.]+) E\d", text)]
        ro, ri = radii(outer), radii(inner)
        self.assertTrue(all(a >= b - 0.01 for a, b in zip(ro, ro[1:])))   # outer: inwards
        self.assertTrue(all(a <= b + 0.01 for a, b in zip(ri, ri[1:])))   # inner: outwards

    def test_jump_to_the_centre_is_a_proper_travel(self):
        new = self.check_split(sample_gcode())
        between = new.split(ft.TOP_MARK)[1].strip().split("\n")[-5:]
        self.assertEqual(between[0], "G1 E-0.8 F1800")       # retract (preset length and speed)
        self.assertEqual(between[1], "G1 Z2 F30000")         # hop by the preset z-hop
        x, y = re.match(r"G1 X([\d.]+) Y([\d.]+)", between[2]).groups()
        self.assertLess(radius((float(x), float(y))), 0.5)   # over to the centre
        self.assertEqual(between[3:], ["G1 Z1.6", "G1 E0.8 F1800"])

    def test_wipe_runs_back_along_the_last_line(self):
        new = self.check_split(sample_gcode())
        wipe = new.split("; WIPE_START", 1)[1].split("; WIPE_END", 1)[0]
        moves = [tuple(map(float, m)) for m in re.findall(r"G1 X([\d.]+) Y([\d.]+) E(-[\d.]+)", wipe)]
        self.assertAlmostEqual(sum(m[2] for m in moves), -0.76, places=4)
        ring = ft.SPLIT * 14.4
        for x, y, _ in moves:
            self.assertAlmostEqual(radius((x, y)), ring, delta=0.5)   # at the ring, not the centre

    def test_other_top_surfaces_left_alone(self):
        src = sample_gcode().replace("G1 X150 Y150 E1", "\n".join([
            "; FEATURE: Top surface", "G1 X150 Y150 E1", "G1 X151 Y150 F30000", "G1 X152 Y150 E1"]))
        new, count = ft.split_spirals(src)
        self.assertEqual(count, 1)
        self.assertIn("; FEATURE: Top surface\nG1 X150 Y150 E1\nG1 X151 Y150 F30000\nG1 X152 Y150 E1", new)

    def test_unexpected_lines_refuse(self):
        bad = sample_gcode().replace("M204 S2000", "M106 S255", 1)
        with self.assertRaises(RuntimeError):
            ft.split_spirals(bad)


class ProjectTests(unittest.TestCase):
    def test_one_object_per_patch_with_its_settings(self):
        offs = ft.offsets(-10, 10, 5)
        objects = ft.build_objects(1.0, offs)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "flow.3mf")
            project3mf.write_project(path, objects=objects, center=(100, 100))
            with zipfile.ZipFile(path) as z:
                ms = ET.fromstring(z.read("Metadata/model_settings.config"))
                model = ET.fromstring(z.read("3D/3dmodel.model"))
        found = []
        for obj in ms.findall("./object"):
            meta = {m.get("key"): m.get("value") for m in obj.findall("metadata")}
            found.append((meta["print_flow_ratio"], meta["top_surface_pattern"], len(obj.findall("part"))))
        self.assertEqual(found, [("0.9", "archimedeanchords", 2), ("0.95", "archimedeanchords", 2), ("1", "archimedeanchords", 2),
                                 ("1.05", "archimedeanchords", 2), ("1.1", "archimedeanchords", 2)])
        items = model.findall("./m:build/m:item", NS)
        self.assertEqual(len(items), 5)
        xs = sorted({round(float(i.get("transform").split()[9]), 3) for i in items})
        self.assertEqual(len(xs), ft.layout(5)[0])
        self.assertEqual(len(ms.findall("./plate/model_instance")), 5)

    def test_pads_are_round(self):
        pad = ft.sample_parts("1.00")[0][2]
        ys = [v[1] for v in pad.verts]
        cy = (min(ys) + max(ys)) / 2
        radii = [math.hypot(v[0], v[1] - cy) for v in pad.verts]
        self.assertAlmostEqual(min(radii), ft.PAD / 2, places=6)
        self.assertAlmostEqual(max(radii), ft.PAD / 2, places=6)
        self.assertEqual(len(pad.verts), 2 * ft.PAD_SEGMENTS)

    def test_label_text_stands_on_the_tab(self):
        parts = ft.sample_parts("0.98")
        tab = parts[1][2]
        zs = [v[2] for v in tab.verts]
        self.assertAlmostEqual(max(zs), ft.TAB_H + ft.TEXT_RAISE, places=4)


class GenerateTests(unittest.TestCase):
    def test_end_to_end_with_fake_slicer(self):
        store = qp.PresetStore(FIXTURE)
        fake = [sys.executable, os.path.join(TESTS, "fake_slicer.py")]
        with tempfile.TemporaryDirectory() as d, \
                mock.patch("studio.cli_command", return_value=fake), \
                mock.patch.dict(os.environ, {"FAKE_SLICER_TOP": "1", "FAKE_SLICER_HEIGHT": "1.6"}):
            out = os.path.join(d, "flow.gcode")
            r = ft.generate(out, [0.0], store=store, base_flow=0.95, nozzle_temp=205, **PRESETS)
            with open(out, encoding="utf-8") as f:
                gcode = f.read()
        self.assertEqual(r["labels"], ["0.95"])
        self.assertEqual(r["reordered"], 1)
        self.assertIn("; FLOW_TEST: base flow ratio 0.95", gcode)
        self.assertIn("; nozzle_temperature = 205", gcode)
        (o_start, o_end), (i_start, i_end) = ft.top_surface_paths(gcode)[0]
        self.assertGreater(o_start, o_end)
        self.assertLess(i_start, i_end)

    def test_too_many_patches_for_the_bed(self):
        store = qp.PresetStore(FIXTURE)
        with self.assertRaises(ValueError):
            ft.generate("x.gcode", ft.offsets(-40, 40, 2), store=store, **PRESETS)


if __name__ == "__main__":
    unittest.main()
