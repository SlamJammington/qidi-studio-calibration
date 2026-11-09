"""Remember the app's settings between runs, in a small JSON file of its own."""
import json
import os
import sys


def settings_path():
    home = os.path.expanduser("~")
    if sys.platform == "win32":
        folder = os.path.join(os.environ.get("APPDATA") or home, "QIDI Studio Calibration")
    elif sys.platform == "darwin":
        folder = os.path.join(home, "Library", "Application Support", "QIDI Studio Calibration")
    else:
        folder = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config"),
                              "qidi-studio-calibration")
    return os.path.join(folder, "settings.json")


def load(path=None):
    """The saved settings, or nothing if there are none or the file is damaged."""
    try:
        with open(path or settings_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(values, path=None):
    """Save the settings. Returns False if that failed: not being remembered shouldn't stop the app."""
    path = path or settings_path()
    temporary = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(temporary, "w", encoding="utf-8") as f:
            json.dump(values, f, indent=1)
        os.replace(temporary, path)  # a crash mid-write can't damage the saved settings
    except OSError:
        return False
    return True
