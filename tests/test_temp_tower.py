import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import qidi_profiles as qp  # noqa: E402
import temp_tower as tt  # noqa: E402
import tower_geometry as geo  # noqa: E402

FIXTURE = os.path.join(ROOT, "tests", "fixtures", "QIDIStudio")
NS = {"m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"}


class PresetTests(unittest.TestCase):
    def setUp(self):
        self.store = qp.PresetStore(FIXTURE)

    def test_inheritance_chain(self):
        f = self.store.resolve("filament", "My PLA")
        self.assertEqual(f["nozzle_temperature"], ["205"])           # user override
        self.assertEqual(f["nozzle_temperature_initial_layer"], ["215"])  # from the abstract parent
        self.assertEqual(f["filament_id"], "TST01")                  # from the system preset

    def test_only_added_printer_models_are_listed(self):
        # QIDIStudio.conf "models" lists Test Printer only, so Other Printer stays hidden like in QIDI Studio.
        self.assertEqual(self.store.names("machine"), ["My Printer", "Test Printer 0.4 nozzle"])

    def test_abstract_presets_hidden(self):
        self.assertNotIn("fdm_filament_test_common", self.store.names("filament"))
        self.assertIn("My PLA", self.store.names("filament"))

    def test_compatibility_uses_system_parent(self):
        self.assertTrue(self.store.compatible("filament", "My PLA", "My Printer"))
        self.assertFalse(self.store.compatible("filament", "Other PLA", "My Printer"))

    def test_selected_presets_and_trailing_checksum(self):
        self.assertEqual(self.store.selected(), ("My Printer", "0.20mm Standard @Test", ["My PLA"]))
        self.assertEqual(self.store.app_version(), "02.07.02.60")

    def test_bed_shape(self):
        self.assertEqual(qp.bed_shape(self.store.resolve("machine", "My Printer")), (0, 0, 250, 200))


class TowerTests(unittest.TestCase):
    def test_temperatures(self):
        self.assertEqual(tt.temperatures(230, 210, 5), [230, 225, 220, 215, 210])
        self.assertEqual(tt.temperatures(240, 260, 10), [240, 250, 260])
        self.assertEqual(tt.temperatures(200, 200, 5), [200])
        with self.assertRaises(ValueError):
            tt.temperatures(230, 212, 5)
        with self.assertRaises(ValueError):
            tt.temperatures(230, 210, 0)

    def test_change_lands_on_first_layer_of_each_floor(self):
        self.assertEqual([tt.layer_print_z(0.2, 0.2, 1 + 10 * k) for k in range(3)], [1.2, 11.2, 21.2])
        # 0.16 mm layers: the layer at 11.08 is sliced at z 11.0, which still belongs to the floor below.
        self.assertEqual(tt.layer_print_z(0.2, 0.16, 11.0), 11.24)

    def test_labels_fit_on_the_label_block(self):
        for k, t in enumerate((99, 230, 1000)):
            m = geo.label_mesh(str(t), geo.floor_bottom(k))
            xs = [v[0] for v in m.verts]
            ys = [v[1] for v in m.verts]
            zs = [v[2] for v in m.verts]
            self.assertLessEqual(max(xs) - min(xs), geo.LABEL_MAX_WIDTH + 1e-6)
            self.assertGreater(min(xs), 0)
            self.assertLess(max(xs), geo.BLOCK_W)
            self.assertLess(min(ys), 0)                      # pokes out of the front face
            self.assertAlmostEqual(max(ys), 1.0, places=3)   # 1 mm deep
            self.assertGreater(min(zs), geo.floor_bottom(k))
            self.assertLess(max(zs), geo.floor_bottom(k + 1))


class GeometryTests(unittest.TestCase):
    def test_primitives_face_outwards(self):
        for m in (geo.box(0, 0, 0, 1, 2, 3), geo.xz_prism([(0, 0), (0, 1), (-1, 1)], 0, 1),
                  geo.cone(0, 0, 1, 0, 2), geo.cylinder_y(0, 0, 1, 0, 2), geo.cylinder_z(0, 0, 1, 0, 2)):
            self.assertGreater(m.volume(), 0)

    def test_every_triangle_edge_is_shared(self):
        # Each shell is closed: every directed edge appears once, and its reverse once.
        m = geo.floor_mesh(0)
        edges = {}
        for a, b, c in m.tris:
            for e in ((a, b), (b, c), (c, a)):
                edges[e] = edges.get(e, 0) + 1
        self.assertTrue(all(n == 1 for n in edges.values()))
        self.assertTrue(all((b, a) in edges for a, b in edges))

    def test_tower_is_centred_and_stacked(self):
        parts = geo.build_parts([230, 220, 210])
        floors = [m for _, sub, m in parts if sub == "normal_part"]
        xs = [v[0] for m in floors for v in m.verts]
        ys = [v[1] for m in floors for v in m.verts]
        self.assertAlmostEqual(min(xs) + max(xs), 0, places=6)
        self.assertAlmostEqual(min(ys) + max(ys), 0, places=6)
        tops = [max(v[2] for v in m.verts) for m in floors]
        self.assertEqual([round(t, 6) for t in tops], [11, 21, 31])
        self.assertEqual(geo.tower_height(3), 31)


class ThreeMFTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = qp.PresetStore(FIXTURE)
        cls.tmp = tempfile.TemporaryDirectory()
        cls.path = os.path.join(cls.tmp.name, "tower & test.3mf")
        cls.temps = tt.temperatures(225, 195, 10)
        cls.result = tt.write_3mf(cls.path, cls.temps, store=cls.store, machine="My Printer",
                                  process="0.20mm Standard @Test", filament="My PLA")
        cls.zip = zipfile.ZipFile(cls.path)

    @classmethod
    def tearDownClass(cls):
        cls.zip.close()
        cls.tmp.cleanup()

    def xml(self, name):
        return ET.fromstring(self.zip.read(name))

    def test_all_xml_parses(self):
        for name in self.zip.namelist():
            if name.endswith((".xml", ".rels", ".model", "slice_info.config", "model_settings.config")):
                self.xml(name)

    def test_parts(self):
        parts = self.xml("Metadata/model_settings.config").findall("./object/part")
        subtypes = [p.get("subtype") for p in parts]
        self.assertEqual(subtypes.count("normal_part"), len(self.temps))
        self.assertEqual(subtypes.count("negative_part"), len(self.temps) + 1)  # labels + holes
        objects = self.xml("3D/Objects/object_1.model").findall("./m:resources/m:object", NS)
        comps = self.xml("3D/3dmodel.model").findall(".//m:component", NS)
        self.assertEqual(len(objects), len(parts))
        self.assertEqual(len(comps), len(parts))

    def test_supports_off_for_the_tower(self):
        obj = self.xml("Metadata/model_settings.config").find("./object")
        meta = {m.get("key"): m.get("value") for m in obj.findall("metadata")}
        self.assertEqual(meta["enable_support"], "0")

    def test_centred_on_bed(self):
        item = self.xml("3D/3dmodel.model").find("./m:build/m:item", NS)
        self.assertEqual(item.get("transform").split()[-3:], ["125.0000", "100.0000", "0"])

    def test_custom_gcode(self):
        layers = self.xml("Metadata/custom_gcode_per_layer.xml").findall("./plate/layer")
        self.assertEqual([float(l.get("top_z")) for l in layers], [1.2, 11.2, 21.2, 31.2])
        self.assertTrue(all(l.get("type") == "4" for l in layers))
        self.assertEqual([l.get("extra").split(" ;")[0] for l in layers],
                         ["M104 S225", "M104 S215", "M104 S205", "M104 S195"])

    def test_project_settings(self):
        cfg = json.loads(self.zip.read("Metadata/project_settings.config"))
        self.assertEqual(cfg["printer_settings_id"], "My Printer")
        self.assertEqual(cfg["print_settings_id"], "0.20mm Standard @Test")
        self.assertEqual(cfg["filament_settings_id"], ["My PLA"])
        self.assertEqual(cfg["inherits_group"], ["", "Test PLA", "Test Printer 0.4 nozzle"])
        # Preset values go in untouched so QIDI Studio matches the presets on load.
        self.assertEqual(cfg["nozzle_temperature"], ["205"])
        self.assertEqual(cfg["curr_bed_type"], "Textured PEI Plate")
        self.assertNotIn("inherits", cfg)
        self.assertNotIn("compatible_printers", cfg)

    def test_too_tall(self):
        with self.assertRaises(ValueError):
            tt.write_3mf(os.path.join(self.tmp.name, "tall.3mf"), tt.temperatures(300, 180, 5),
                         store=self.store, machine="My Printer", process="0.20mm Standard @Test",
                         filament="My PLA")

    def test_no_settings(self):
        p = os.path.join(self.tmp.name, "bare.3mf")
        tt.write_3mf(p, self.temps, store=self.store, machine="My Printer",
                     process="0.20mm Standard @Test", filament="My PLA", embed_settings=False)
        with zipfile.ZipFile(p) as z:
            self.assertNotIn("Metadata/project_settings.config", z.namelist())


class CliTests(unittest.TestCase):
    def test_cli_defaults_to_selected_presets(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "cli.3mf")
            r = subprocess.run([sys.executable, os.path.join(ROOT, "qidi_studio_calibration.py"), "--qidi-dir", FIXTURE,
                                "-o", out], capture_output=True, text=True, encoding="utf-8",
                               env=dict(os.environ, PYTHONIOENCODING="utf-8"))
            self.assertEqual(r.returncode, 0, r.stderr)
            with zipfile.ZipFile(out) as z:
                cfg = json.loads(z.read("Metadata/project_settings.config"))
            self.assertEqual(cfg["filament_settings_id"], ["My PLA"])
            self.assertIn("7 floors", r.stdout)  # My PLA's 225 -> 195 range in 5 degree steps

    def test_cli_list(self):
        r = subprocess.run([sys.executable, os.path.join(ROOT, "qidi_studio_calibration.py"), "--qidi-dir", FIXTURE,
                            "--list", "filament"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.split(), ["My", "PLA", "Test", "PLA"])


if __name__ == "__main__":
    unittest.main()
