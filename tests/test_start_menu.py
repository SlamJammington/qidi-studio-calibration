import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import app_settings  # noqa: E402
import start_menu as sm  # noqa: E402


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "folder", "settings.json")

    def test_nothing_saved_yet(self):
        self.assertEqual(app_settings.load(self.path), {})

    def test_saves_and_loads(self):
        self.assertTrue(app_settings.save({"filament": "My PLA °C", "wait": True}, self.path))
        self.assertEqual(app_settings.load(self.path), {"filament": "My PLA °C", "wait": True})
        self.assertEqual(os.listdir(os.path.dirname(self.path)), ["settings.json"])

    def test_damaged_file_is_ignored(self):
        os.makedirs(os.path.dirname(self.path))
        for text in ("{not json", "[1, 2]", ""):
            with open(self.path, "w", encoding="utf-8") as f:
                f.write(text)
            self.assertEqual(app_settings.load(self.path), {})

    def test_failing_to_save_is_not_an_error(self):
        with open(os.path.join(self.tmp.name, "file"), "w", encoding="utf-8"):
            pass
        self.assertFalse(app_settings.save({}, os.path.join(self.tmp.name, "file", "settings.json")))


class MenuTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        folder = self.tmp.name
        env = mock.patch.dict(os.environ, {"APPDATA": os.path.join(folder, "roaming"),
                                           "LOCALAPPDATA": os.path.join(folder, "local"),
                                           "XDG_DATA_HOME": os.path.join(folder, "share")})
        env.start()
        self.addCleanup(env.stop)
        self.exe = os.path.join(folder, "Downloads", "QidiStudioCalibration-1.2.6-windows.exe")
        os.makedirs(os.path.dirname(self.exe))
        with open(self.exe, "wb") as f:
            f.write(b"the app")

    def use_platform(self, name):
        patcher = mock.patch.object(sm.sys, "platform", name)
        patcher.start()
        self.addCleanup(patcher.stop)


class CopyTests(MenuTestCase):
    def setUp(self):
        super().setUp()
        self.use_platform("linux")  # no shortcut tool needed

    def test_only_downloaded_builds_on_windows_and_linux(self):
        self.assertFalse(sm.available())  # running from source
        for platform, expected in (("win32", True), ("linux", True), ("darwin", False)):
            with mock.patch.object(sm.sys, "frozen", True, create=True), \
                    mock.patch.object(sm.sys, "platform", platform):
                self.assertEqual(sm.available(), expected, platform)

    def test_copy_has_no_version_in_its_name(self):
        target = sm.add(self.exe)
        self.assertEqual(os.path.basename(target), "QidiStudioCalibration")
        with open(target, "rb") as f:
            self.assertEqual(f.read(), b"the app")
        self.assertTrue(os.path.exists(self.exe), "the original stays until the user deletes it")
        self.assertEqual(os.listdir(os.path.dirname(target)), ["QidiStudioCalibration"])

    def test_is_added_only_for_the_copy_with_its_shortcut(self):
        self.assertFalse(sm.is_added(self.exe))
        target = sm.add(self.exe)
        self.assertFalse(sm.is_added(self.exe))
        self.assertTrue(sm.is_added(target))
        os.remove(sm.shortcut_path())
        self.assertFalse(sm.is_added(target))

    def test_the_copy_can_add_its_own_shortcut_back(self):
        target = sm.add(self.exe)
        os.remove(sm.shortcut_path())
        self.assertEqual(sm.add(target), target)
        with open(target, "rb") as f:
            self.assertEqual(f.read(), b"the app")
        self.assertTrue(sm.is_added(target))

    def test_a_running_copy_gives_a_plain_message(self):
        with mock.patch.object(sm.os, "replace", side_effect=PermissionError("in use")), \
                self.assertRaisesRegex(RuntimeError, "Close it"):
            sm.add(self.exe)
        self.assertEqual(os.listdir(os.path.dirname(sm.installed_path())), [], "no half-finished copy left")


class LinuxTests(MenuTestCase):
    def test_desktop_entry(self):
        self.use_platform("linux")
        icon = os.path.join(self.tmp.name, "source.png")
        with open(icon, "wb") as f:
            f.write(b"png")
        target = sm.add(self.exe, icon_source=icon)
        with open(sm.shortcut_path(), encoding="utf-8") as f:
            text = f.read()
        self.assertIn('Exec="%s"' % target.replace("\\", "\\\\\\\\"), text)
        self.assertIn("Icon=" + os.path.join(os.path.dirname(target), "icon.png"), text)
        self.assertIn("Terminal=false", text)

    def test_special_characters_in_the_path(self):
        self.assertIn(r'Exec="/home/a b/100%%/\\$x"', sm.desktop_entry("/home/a b/100%/$x"))


@unittest.skipUnless(sys.platform == "win32", "Windows shortcut")
class WindowsTests(MenuTestCase):
    def assertSamePath(self, a, b):  # noqa: N802
        # Windows may write a shortened folder name (RUNNER~1) out in full in the shortcut.
        self.assertEqual(os.path.normcase(os.path.realpath(a)), os.path.normcase(os.path.realpath(b)))

    def read_shortcut(self):
        script = ("[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
                  "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:QSC_LINK); "
                  "@{target=$s.TargetPath; folder=$s.WorkingDirectory; icon=$s.IconLocation} | ConvertTo-Json")
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], check=True,
                             capture_output=True, encoding="utf-8", env=dict(os.environ, QSC_LINK=sm.shortcut_path()))
        return json.loads(out.stdout)

    def test_real_shortcut(self):
        target = sm.add(self.exe)
        self.assertEqual(sm.shortcut_path(), os.path.join(
            os.environ["APPDATA"], "Microsoft", "Windows", "Start Menu", "Programs", "QIDI Studio Calibration.lnk"))
        self.assertEqual(target, os.path.join(os.environ["LOCALAPPDATA"], "Programs", "QIDI Studio Calibration",
                                              "QidiStudioCalibration.exe"))
        link = self.read_shortcut()
        self.assertSamePath(link["target"], target)
        self.assertSamePath(link["folder"], os.path.dirname(target))
        icon, index = link["icon"].rsplit(",", 1)
        self.assertSamePath(icon, target)
        self.assertEqual(index, "0")

    def test_awkward_characters_in_the_path(self):
        os.environ["LOCALAPPDATA"] = os.path.join(self.tmp.name, "O'Brien $HOME & co (é)")
        target = sm.add(self.exe)
        self.assertSamePath(self.read_shortcut()["target"], target)

    def test_shortcut_failure_is_reported(self):
        with mock.patch.dict(os.environ, {"SystemRoot": os.path.join(self.tmp.name, "nowhere")}), \
                self.assertRaisesRegex(RuntimeError, "shortcut"):
            sm.add(self.exe)


if __name__ == "__main__":
    unittest.main()
