import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(ROOT, "tests")
sys.path.insert(0, ROOT)
sys.path.insert(0, TESTS)
import qidi_box as qb  # noqa: E402
import qidi_profiles as qp  # noqa: E402
import retraction_test as rt  # noqa: E402
from sliced_gcode import save_gcode  # noqa: E402

FIXTURE = os.path.join(TESTS, "fixtures", "QIDIStudio")
PRESETS = dict(machine="My Printer", process="0.20mm Standard @Test", filament="My PLA")

FILAS = """[fila7]
filament = PLA Basic
type = PLA

[fila39]
filament = PETG Basic
type = PETG

[colordict]
1 = #FAFAFA
2 = #060606
9 = #228332

[vendor_list]
0 = Generic
1 = QIDI
"""

# A one-Box printer: slot 1 empty, 2 and 3 black PLA Basic, 4 green PETG Basic.
SAVED = {"filament_slot0": 7, "filament_slot1": 7, "filament_slot2": 7, "filament_slot3": 39,
         "color_slot0": 1, "color_slot1": 2, "color_slot2": 2, "color_slot3": 9,
         "vendor_slot0": 0, "vendor_slot1": 0, "vendor_slot2": 0, "vendor_slot3": 1,
         "slot0": 0, "slot1": 1, "slot2": 1, "slot3": 1}
RUNOUT = {0: 1, 1: 0, 2: 0, 3: 0}     # the sensor reads 1 when a slot is empty


class FakeKlipper(BaseHTTPRequestHandler):
    has_box = True

    def reply(self, body, status=200, kind="application/json"):
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        url = urllib.parse.urlparse(self.path)
        if url.path == "/printer/objects/list":
            objs = ["toolhead", "save_variables"]
            if FakeKlipper.has_box:
                objs += ["box_extras"] + ["box_stepper slot%d" % i for i in range(4)]
            return self.reply({"result": {"objects": objs}})
        if url.path == "/printer/objects/query":
            names = [urllib.parse.unquote(q) for q in url.query.split("&")]
            status = {}
            for n in names:
                if n == "save_variables":
                    status[n] = {"variables": SAVED}
                elif n.startswith("box_stepper slot"):
                    status[n] = {"runout_button": RUNOUT[int(n[-1])], "rfid_state": 0}
            return self.reply({"result": {"status": status}})
        if url.path == "/server/files/config/officiall_filas_list.cfg":
            return self.reply(FILAS, kind="text/plain")
        self.reply({"error": "not found"}, 404)

    def log_message(self, *args):
        pass


class BoxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeKlipper)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.printer = {"name": "Fake", "host_type": "octoprint", "print_host": "127.0.0.1",
                       "printhost_port": str(cls.server.server_port)}

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        FakeKlipper.has_box = True

    def test_reads_the_slots(self):
        slots = qb.read_box(self.printer)
        self.assertEqual([(s.number, s.material, s.colour, s.vendor, s.loaded) for s in slots], [
            (1, "PLA Basic", "#FAFAFA", "Generic", False),
            (2, "PLA Basic", "#060606", "Generic", True),
            (3, "PLA Basic", "#060606", "Generic", True),
            (4, "PETG Basic", "#228332", "QIDI", True)])
        self.assertEqual(slots[0].describe(), "Slot 1: empty")
        self.assertEqual(slots[1].describe(), "Slot 2: PLA Basic (Generic)")
        self.assertEqual(slots[3].describe(), "Slot 4: QIDI PETG Basic")

    def test_no_box(self):
        FakeKlipper.has_box = False
        self.assertIsNone(qb.read_box(self.printer))

    def test_unreachable_printer_raises(self):
        with self.assertRaises(OSError):
            qb.read_box(dict(self.printer, printhost_port="1"), timeout=2)

    def test_default_slot_matches_the_filament_type(self):
        slots = qb.read_box(self.printer)
        self.assertEqual(qb.default_slot(slots, "PLA").number, 2)    # slot 1 is empty
        self.assertEqual(qb.default_slot(slots, "PETG").number, 4)
        self.assertEqual(qb.default_slot(slots, "ABS").number, 2)    # nothing matches: first loaded
        self.assertIsNone(qb.default_slot([s for s in slots if not s.loaded]))

    def test_command_matches_qidi_studio(self):
        self.assertEqual(qb.select_slot_gcode(2), "SAVE_VARIABLE VARIABLE=value_t0 VALUE=\"'slot2'\"")


class GcodeTests(unittest.TestCase):
    def test_slot_is_chosen_before_the_box_loads(self):
        gcode = "\n".join(["; HEADER_BLOCK_START", "; HEADER_BLOCK_END", "; EXECUTABLE_BLOCK_START",
                           "BOX_PRINT_START EXTRUDER=0 HOTENDTEMP=240", "T0", ""])
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "x.gcode")
            save_gcode(path, gcode, start_lines=["; QIDI Box: print from slot 3", qb.select_slot_gcode(2)])
            with open(path, encoding="utf-8") as f:
                lines = f.read().split("\n")
        i = lines.index(qb.select_slot_gcode(2))
        self.assertEqual(lines[i - 2:i], ["; EXECUTABLE_BLOCK_START", "; QIDI Box: print from slot 3"])
        self.assertLess(i, lines.index("BOX_PRINT_START EXTRUDER=0 HOTENDTEMP=240"))

    def test_test_pieces_carry_the_slot(self):
        store = qp.PresetStore(FIXTURE)
        fake = [sys.executable, os.path.join(TESTS, "fake_slicer.py")]
        with tempfile.TemporaryDirectory() as d, mock.patch("studio.cli_command", return_value=fake), \
                mock.patch.dict(os.environ, {"FAKE_SLICER_HEIGHT": "6"}):
            out = os.path.join(d, "r.gcode")
            rt.generate(out, [0.0, 1.0], store=store, box_slot=3, **PRESETS)
            with open(out, encoding="utf-8") as f:
                with_slot = f.read()
            rt.generate(out, [0.0, 1.0], store=store, **PRESETS)
            with open(out, encoding="utf-8") as f:
                without = f.read()
        self.assertIn(qb.select_slot_gcode(3), with_slot)
        self.assertNotIn("SAVE_VARIABLE", without)

    def test_cli_option(self):
        fake = [sys.executable, os.path.join(TESTS, "fake_slicer.py")]
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "c.gcode")
            env = dict(os.environ, PYTHONIOENCODING="utf-8", FAKE_SLICER_HEIGHT="6")
            code = ("import sys; from unittest import mock; sys.argv = sys.argv[1:];"
                    "import runpy; mock.patch('studio.cli_command', return_value=%r).start();"
                    "runpy.run_path(%r, run_name='__main__')" % (fake, os.path.join(ROOT, "qidi_studio_calibration.py")))
            r = subprocess.run([sys.executable, "-c", code, "x", "--qidi-dir", FIXTURE, "--test", "retraction",
                                "--end", "1", "--step", "1", "--box-slot", "4", "-o", out],
                               capture_output=True, text=True, env=env, cwd=ROOT, timeout=120)
            self.assertEqual(r.returncode, 0, r.stderr)
            with open(out, encoding="utf-8") as f:
                self.assertIn(qb.select_slot_gcode(3), f.read())
            bad = subprocess.run([sys.executable, os.path.join(ROOT, "qidi_studio_calibration.py"), "--qidi-dir",
                                  FIXTURE, "--test", "retraction", "--box-slot", "0"], capture_output=True,
                                 text=True, env=env, timeout=60)
            self.assertEqual(bad.returncode, 1)
            self.assertIn("--box-slot must be between 1 and 16", bad.stderr)


if __name__ == "__main__":
    unittest.main()
