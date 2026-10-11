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


def sample_gcode():
    head = ["; HEADER_BLOCK_START", "; HEADER_BLOCK_END", "M83", "; MACHINE_START_GCODE_END",
            "; CHANGE_LAYER", "; Z_HEIGHT: 1.6", "; LAYER_HEIGHT: 0.2", "G1 X90 Y90 F30000"]
    return "\n".join(head + fake_slicer.top_surface() + ["G1 X150 Y150 E1", "; MACHINE_END_GCODE_START", ""])


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


class ReverseTests(unittest.TestCase):
    def setUp(self):
        self.src = sample_gcode()
        self.new, self.count, self.kept = ft.top_surfaces_outside_in(self.src)

    def test_rings_now_run_outside_in(self):
        self.assertEqual(self.count, 1)
        before = ft.ring_order(self.src, (100, 100))
        after = ft.ring_order(self.new, (100, 100))
        self.assertEqual(before, sorted(before))                 # QIDI Studio: inside out
        self.assertEqual(after, sorted(after, reverse=True))     # now: outside in
        self.assertEqual(sorted(before), sorted(after))

    def test_extrusion_and_state_kept(self):
        self.assertAlmostEqual(body_extrusion(self.new), body_extrusion(self.src), places=5)
        top = self.new.split(ft.TOP_MARK, 1)[1].split("; WIPE_START", 1)[0]
        self.assertIn("; LINE_WIDTH: 0.43", top)          # per-ring line widths travel with their ring
        self.assertEqual(top.strip().split("\n")[0], "; LINE_WIDTH: 0.43")

    def test_travel_in_lands_on_the_outer_ring(self):
        before_top = self.new.split(ft.TOP_MARK, 1)[0].strip().split("\n")
        # the z-hopped travel is redirected; the nozzle lowers and un-retracts at the outside
        self.assertEqual(before_top[-3:], ["G1 X101.6 Y100 Z2 F30000", "G1 Z1.6", "G1 E.8 F1800"])

    def test_wipe_runs_over_the_innermost_ring(self):
        wipe = self.new.split("; WIPE_START", 1)[1].split("; WIPE_END", 1)[0]
        moves = [tuple(map(float, m)) for m in re.findall(r"G1 X([\d.]+) Y([\d.]+) E(-[\d.]+)", wipe)]
        self.assertTrue(moves)
        self.assertAlmostEqual(sum(m[2] for m in moves), -0.76, places=4)
        for x, y, _ in moves:
            self.assertLessEqual(max(abs(x - 100), abs(y - 100)), 0.4 + 1e-6)
        # wipe length kept: 1.6 mm, along the 0.4 mm ring
        pts = [(100.4, 100)] + [(x, y) for x, y, _ in moves]
        length = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(pts, pts[1:]))
        self.assertAlmostEqual(length, 1.6, places=3)

    def test_outside_in_surfaces_are_left_alone(self):
        once = self.new
        twice, count, kept = ft.top_surfaces_outside_in(once)
        self.assertEqual((count, kept), (0, 1))
        self.assertEqual(twice, once)
        self.assertEqual(ft.top_surface_directions(once), ["in"])
        self.assertEqual(ft.top_surface_directions(self.src), ["out"])

    def test_unexpected_lines_refuse(self):
        bad = self.src.replace("M204 S2000", "M106 S255", 1)
        with self.assertRaises(RuntimeError):
            ft.top_surfaces_outside_in(bad)


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
        self.assertEqual(found, [("0.9", "concentric", 2), ("0.95", "concentric", 2), ("1", "concentric", 2),
                                 ("1.05", "concentric", 2), ("1.1", "concentric", 2)])
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
        self.assertEqual((r["reversed"], r["kept"]), (1, 0))
        self.assertIn("; FLOW_TEST: base flow ratio 0.95", gcode)
        self.assertIn("; nozzle_temperature = 205", gcode)
        runs = ft.ring_order(gcode, (100, 100))
        self.assertEqual(runs, sorted(runs, reverse=True))

    def test_too_many_patches_for_the_bed(self):
        store = qp.PresetStore(FIXTURE)
        with self.assertRaises(ValueError):
            ft.generate("x.gcode", ft.offsets(-40, 40, 2), store=store, **PRESETS)


if __name__ == "__main__":
    unittest.main()
