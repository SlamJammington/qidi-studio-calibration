"""Add the app to the Windows Start menu or the Linux app menu.

The shortcut points at a copy in a fixed place, with no version in its file name. So it keeps
working after the file the user downloaded is moved or deleted, and after every update.
"""
import os
import re
import shutil
import subprocess
import sys

from updater import BINARY_NAME

APP_TITLE = "QIDI Studio Calibration"

# Windows' own tool for making shortcuts. The paths arrive as environment variables, so no
# character in them can break the script.
MAKE_SHORTCUT = r"""
$link = (New-Object -ComObject WScript.Shell).CreateShortcut($env:QSC_LINK)
$link.TargetPath = $env:QSC_TARGET
$link.WorkingDirectory = Split-Path $env:QSC_TARGET
$link.IconLocation = "$env:QSC_TARGET,0"
$link.Description = $env:QSC_TITLE
$link.Save()
"""


def available():
    """Only downloaded builds on Windows and Linux. macOS apps get dragged to Applications instead."""
    return bool(getattr(sys, "frozen", False)) and sys.platform in ("win32", "linux")


def button_text():
    return "Add to Start menu" if sys.platform == "win32" else "Add to app menu"


def menu_name():
    return "Start menu" if sys.platform == "win32" else "app menu"


def _linux_data_home():
    return os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")


def installed_path():
    """Where the copy that the shortcut starts lives."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
        return os.path.join(base, "Programs", APP_TITLE, BINARY_NAME + ".exe")
    return os.path.join(_linux_data_home(), "qidi-studio-calibration", BINARY_NAME)


def shortcut_path():
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
        return os.path.join(base, "Microsoft", "Windows", "Start Menu", "Programs", APP_TITLE + ".lnk")
    return os.path.join(_linux_data_home(), "applications", "qidi-studio-calibration.desktop")


def _same_file(a, b):
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def is_added(exe=None):
    """Whether this is the copy the menu starts, with its shortcut still there."""
    return _same_file(exe or sys.executable, installed_path()) and os.path.exists(shortcut_path())


def _copy_app(exe, target):
    os.makedirs(os.path.dirname(target), exist_ok=True)
    temporary = target + ".new"
    try:
        shutil.copyfile(exe, temporary)
        os.chmod(temporary, 0o755)
        try:
            os.replace(temporary, target)
        except PermissionError:
            raise RuntimeError("The copy in the %s is running. Close it and try again." % menu_name()) from None
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def _windows_shortcut(target, link):
    powershell = os.path.join(os.environ.get("SystemRoot") or r"C:\Windows", "System32",
                              "WindowsPowerShell", "v1.0", "powershell.exe")
    env = dict(os.environ, QSC_LINK=link, QSC_TARGET=target, QSC_TITLE=APP_TITLE)
    try:
        proc = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-Command", MAKE_SHORTCUT],
                              capture_output=True, text=True, errors="replace", timeout=60, env=env,
                              creationflags=0x08000000)  # CREATE_NO_WINDOW
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError("Couldn't create the shortcut: %s" % e) from None
    if proc.returncode != 0 or not os.path.exists(link):
        raise RuntimeError("Couldn't create the shortcut: %s" % (proc.stderr.strip() or "Windows didn't make it"))


def desktop_entry(target, icon=None):
    """The text of a Linux .desktop file, which quotes paths its own way."""
    command = '"%s"' % re.sub(r'([\\"`$])', r"\\\1", target).replace("\\", "\\\\").replace("%", "%%")
    lines = ["[Desktop Entry]", "Type=Application", "Name=" + APP_TITLE,
             "Comment=Calibration prints for QIDI Studio", "Exec=" + command]
    if icon:
        lines.append("Icon=" + icon)
    return "\n".join(lines + ["Terminal=false", "Categories=Utility;", ""])


def _linux_shortcut(target, link, icon_source):
    icon = None
    if icon_source and os.path.exists(icon_source):
        icon = os.path.join(os.path.dirname(target), "icon.png")
        shutil.copyfile(icon_source, icon)
    os.makedirs(os.path.dirname(link), exist_ok=True)
    with open(link, "w", encoding="utf-8") as f:
        f.write(desktop_entry(target, icon))
    os.chmod(link, 0o755)


def add(exe=None, icon_source=None):
    """Copy the app to its fixed place, unless it's already running from there, and add the
    shortcut. Returns the path of the copy."""
    exe = exe or sys.executable
    target, link = installed_path(), shortcut_path()
    if not _same_file(exe, target):
        _copy_app(exe, target)
    os.makedirs(os.path.dirname(link), exist_ok=True)
    if sys.platform == "win32":
        _windows_shortcut(target, link)
    else:
        _linux_shortcut(target, link, icon_source)
    return target
