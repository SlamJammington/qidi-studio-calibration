import hashlib
import io
import os
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import updater as up  # noqa: E402

NEW_APP = b"the new version of the app"
REAL_CHECK_RUNS = up._check_runs


class FakeGitHub(BaseHTTPRequestHandler):
    files = {}

    def do_GET(self):  # noqa: N802
        data = FakeGitHub.files.get(self.path)
        self.send_response(200 if data is not None else 404)
        self.send_header("Content-Length", str(len(data or b"")))
        self.end_headers()
        self.wfile.write(data or b"")

    def log_message(self, *args):
        pass


class VersionTests(unittest.TestCase):
    def test_versions(self):
        self.assertEqual(up.parse_version("v1.2.10"), (1, 2, 10))
        self.assertTrue(up.is_newer("1.2.10", "1.2.9"))     # numbers, not text
        self.assertTrue(up.is_newer("1.3", "1.2.9"))
        self.assertFalse(up.is_newer("1.2.5", "1.2.5"))
        self.assertFalse(up.is_newer("1.2.4", "1.2.5"))     # never a downgrade
        with self.assertRaises(ValueError):
            up.parse_version("latest")

    def test_finds_the_file_with_or_without_the_version_in_its_name(self):
        for name in ("QidiStudioCalibration-windows.exe", "QidiStudioCalibration-1.2.6-windows.exe"):
            release = up.Release("1.2.6", {"QidiStudioCalibration-1.2.6-macos.zip": "u", name: "u",
                                           "SHA256SUMS.txt": "u"})
            with mock.patch.object(up, "asset_suffix", return_value="windows.exe"):
                self.assertEqual(up.find_asset(release), name)
        with mock.patch.object(up, "asset_suffix", return_value="linux.tar.gz"), self.assertRaises(RuntimeError):
            up.find_asset(release)

    def test_version_in_the_file_name_follows_the_update(self):
        p = os.path.join("folder", "QidiStudioCalibration-1.2.5-windows.exe")
        self.assertEqual(up.target_path(p, "1.2.5", "1.2.6"),
                         os.path.join("folder", "QidiStudioCalibration-1.2.6-windows.exe"))
        p = os.path.join("folder", "Calibration.exe")
        self.assertEqual(up.target_path(p, "1.2.5", "1.2.6"), p)

    def test_only_a_built_app_on_windows_or_linux_replaces_itself(self):
        with mock.patch.object(sys, "frozen", True, create=True):
            for platform, expected in (("win32", True), ("linux", True), ("darwin", False)):
                with mock.patch.object(sys, "platform", platform):
                    self.assertEqual(up.can_replace(), expected, platform)
        self.assertFalse(up.can_replace())      # running from source


class InstallTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeGitHub)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.exe = os.path.join(self.dir, "QidiStudioCalibration-1.2.5-windows.exe")
        with open(self.exe, "wb") as f:
            f.write(b"the running app")
        patcher = mock.patch.object(up, "_check_runs")
        self.check_runs = patcher.start()
        self.addCleanup(patcher.stop)

    def publish(self, name, data, checksum=None):
        FakeGitHub.files = {"/" + name: data, "/sums": ("%s  %s\n" % (
            checksum or hashlib.sha256(data).hexdigest(), name)).encode()}
        return up.Release("1.2.6", {name: self.base + "/" + name, "SHA256SUMS.txt": self.base + "/sums"})

    def read(self, path):
        with open(path, "rb") as f:
            return f.read()

    def install(self, release):
        with mock.patch.object(up, "asset_suffix", return_value="windows.exe"):
            return up.install(release, "1.2.5", exe=self.exe)

    def test_replaces_the_app_and_moves_the_old_one_aside(self):
        path = self.install(self.publish("QidiStudioCalibration-1.2.6-windows.exe", NEW_APP))
        self.assertEqual(os.path.basename(path), "QidiStudioCalibration-1.2.6-windows.exe")
        self.assertEqual(self.read(path), NEW_APP)
        self.assertEqual(sorted(os.listdir(self.dir)), sorted([os.path.basename(path), os.path.basename(self.exe)
                                                               + up.OLD_SUFFIX]))
        self.check_runs.assert_called_once()

    def assert_unchanged(self):
        self.assertEqual(os.listdir(self.dir), [os.path.basename(self.exe)])
        self.assertEqual(self.read(self.exe), b"the running app")

    def test_damaged_download_changes_nothing(self):
        release = self.publish("QidiStudioCalibration-1.2.6-windows.exe", NEW_APP, checksum="0" * 64)
        with self.assertRaisesRegex(RuntimeError, "checksum"):
            self.install(release)
        self.assert_unchanged()
        self.check_runs.assert_not_called()

    def test_refuses_a_release_without_checksums(self):
        release = up.Release("1.2.6", {"QidiStudioCalibration-1.2.6-windows.exe": self.base + "/x"})
        with self.assertRaisesRegex(RuntimeError, "checksums"):
            self.install(release)
        self.assert_unchanged()

    def test_download_that_wont_run_changes_nothing(self):
        self.check_runs.side_effect = RuntimeError("didn't start")
        with self.assertRaises(RuntimeError):
            self.install(self.publish("QidiStudioCalibration-1.2.6-windows.exe", NEW_APP))
        self.assert_unchanged()

    def test_failed_swap_puts_the_app_back(self):
        real = os.replace

        def replace(src, dst):
            if dst.endswith("1.2.6-windows.exe"):
                raise PermissionError("in use")
            real(src, dst)

        release = self.publish("QidiStudioCalibration-1.2.6-windows.exe", NEW_APP)
        with mock.patch.object(up.os, "replace", replace), self.assertRaises(PermissionError):
            self.install(release)
        self.assertEqual(self.read(self.exe), b"the running app")
        self.assertFalse(os.path.exists(self.exe + up.OLD_SUFFIX))

    def test_unpacks_the_linux_archive(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo("QidiStudioCalibration")
            info.size = len(NEW_APP)
            tar.addfile(info, io.BytesIO(NEW_APP))
        release = self.publish("QidiStudioCalibration-1.2.6-linux.tar.gz", buf.getvalue())
        with mock.patch.object(up, "asset_suffix", return_value="linux.tar.gz"):
            path = up.install(release, "1.2.5", exe=self.exe)
        self.assertEqual(self.read(path), NEW_APP)

    def test_checks_that_the_download_reports_the_new_version(self):
        result = mock.Mock(returncode=0, stdout="qidi_studio_calibration 1.2.5")
        with mock.patch.object(up.subprocess, "run", return_value=result) as run:
            with self.assertRaisesRegex(RuntimeError, "didn't start"):
                REAL_CHECK_RUNS("x", "1.2.6")
            REAL_CHECK_RUNS("x", "1.2.5")
        self.assertEqual(run.call_args[1]["env"]["PYINSTALLER_RESET_ENVIRONMENT"], "1")

    def test_clean_up_removes_only_what_an_update_left(self):
        old = self.exe + up.OLD_SUFFIX
        with open(old, "wb") as f:
            f.write(b"old")
        up.clean_up_old(self.dir)
        deadline = time.monotonic() + 5
        while os.path.exists(old) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(self.exe))


if __name__ == "__main__":
    unittest.main()
