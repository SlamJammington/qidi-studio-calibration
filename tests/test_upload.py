import json
import os
import sys
import tempfile
import threading
import unittest
from email.parser import BytesParser
from email.policy import default as email_policy
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import printer_upload as pu  # noqa: E402
import qidi_profiles as qp  # noqa: E402
import qidi_studio_calibration as app  # noqa: E402

FIXTURE = os.path.join(ROOT, "tests", "fixtures", "QIDIStudio")


class FakePrinter(BaseHTTPRequestHandler):
    """Answers OctoPrint-style uploads the way Moonraker does, remembering what it got."""
    received = []

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers["Content-Length"]))
        msg = BytesParser(policy=email_policy).parsebytes(
            b"Content-Type: " + self.headers["Content-Type"].encode() + b"\r\n\r\n" + body)
        fields = {part.get_param("name", header="content-disposition"): part for part in msg.iter_parts()}
        FakePrinter.received.append({
            "path": self.path, "api_key": self.headers.get("X-Api-Key"),
            "filename": fields["file"].get_filename(), "data": fields["file"].get_payload(decode=True),
            "print": fields["print"].get_content().strip()})
        status = 409 if fields["file"].get_filename() == "refuse.gcode" else 201
        reply = json.dumps({"done": True, "print_started": fields["print"].get_content().strip() == "true"})
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(reply.encode())

    def log_message(self, *args):
        pass


class UploadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakePrinter)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.printer = {"name": "Fake", "host_type": "octoprint", "print_host": "127.0.0.1",
                       "printhost_port": str(cls.server.server_port), "printhost_apikey": "secret"}
        cls.tmp = tempfile.TemporaryDirectory()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.tmp.cleanup()

    def setUp(self):
        FakePrinter.received.clear()

    def write(self, name, text="; test\nG28\n"):
        path = os.path.join(self.tmp.name, name)
        with open(path, "wb") as f:
            f.write(text.encode())
        return path

    def test_upload_without_starting(self):
        path = self.write("Retraction Test.gcode")
        reply = pu.upload(self.printer, path)
        got = FakePrinter.received[-1]
        self.assertEqual(got["path"], "/api/files/local")
        self.assertEqual(got["filename"], "Retraction Test.gcode")
        self.assertEqual(got["data"], b"; test\nG28\n")
        self.assertEqual(got["print"], "false")
        self.assertEqual(got["api_key"], "secret")
        self.assertFalse(reply["print_started"])

    def test_upload_and_start(self):
        pu.upload(self.printer, self.write("go.gcode"), start=True)
        self.assertEqual(FakePrinter.received[-1]["print"], "true")

    def test_refused_and_unreachable(self):
        with self.assertRaises(RuntimeError) as e:
            pu.upload(self.printer, self.write("refuse.gcode"))
        self.assertIn("409", str(e.exception))
        dead = dict(self.printer, printhost_port="1")
        with self.assertRaises(RuntimeError):
            pu.upload(dead, self.write("x.gcode"), timeout=5)

    def test_unsupported_host_type(self):
        with self.assertRaises(ValueError):
            pu.upload(dict(self.printer, host_type="duet"), self.write("x.gcode"))

    def test_base_url(self):
        self.assertEqual(pu.base_url({"print_host": "192.168.1.24"}), "http://192.168.1.24")
        self.assertEqual(pu.base_url({"print_host": "printer.local", "printhost_port": "7125"}),
                         "http://printer.local:7125")
        self.assertEqual(pu.base_url({"print_host": "https://p.example/"}), "https://p.example")


class CliErrorTests(unittest.TestCase):
    def test_unknown_printer_is_a_plain_error(self):
        import subprocess
        r = subprocess.run([sys.executable, os.path.join(ROOT, "qidi_studio_calibration.py"), "--qidi-dir", FIXTURE,
                            "--test", "retraction", "--send", "Nope"], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 1)
        self.assertIn("Error: No network printer called 'Nope'", r.stderr)
        self.assertNotIn("Traceback", r.stderr)


class NetworkPrinterTests(unittest.TestCase):
    def test_reads_qidi_studio_network_printers(self):
        store = qp.PresetStore(FIXTURE)
        printers = store.network_printers()
        self.assertEqual([p["name"] for p in printers], ["Workshop Printer"])
        self.assertEqual(app.pick_network_printer(store)["print_host"], "192.0.2.10")
        self.assertEqual(app.pick_network_printer(store, "Workshop Printer")["name"], "Workshop Printer")
        with self.assertRaises(ValueError):
            app.pick_network_printer(store, "Nope")


if __name__ == "__main__":
    unittest.main()
