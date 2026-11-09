"""Update the app to the newest GitHub release, in place.

The Windows and Linux builds are one file, so updating means swapping that file.
A running file can be renamed but not overwritten on Windows, so the running one
is moved aside and removed by the new version once this one has exited. The macOS
app and source checkouts aren't replaced; they get pointed at the download page.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.request

REPO = "SlamJammington/qidi-studio-calibration"
API_URL = "https://api.github.com/repos/%s/releases/latest" % REPO
RELEASES_PAGE = "https://github.com/%s/releases/latest" % REPO
BINARY_NAME = "QidiStudioCalibration"
OLD_SUFFIX = ".update-old"


class Release:
    def __init__(self, version, assets):
        self.version = version      # "1.2.6"
        self.assets = assets        # {file name: download URL}


def parse_version(text):
    """(1, 2, 6) from "v1.2.6" or "1.2.6"."""
    m = re.fullmatch(r"v?(\d+(?:\.\d+)*)", text.strip())
    if not m:
        raise ValueError("Not a version number: %r" % text)
    return tuple(int(n) for n in m.group(1).split("."))


def is_newer(latest, current):
    return parse_version(latest) > parse_version(current)


def asset_suffix():
    """How this system's release file name ends, or None if it can't be swapped in place."""
    return {"win32": "windows.exe", "linux": "linux.tar.gz"}.get(sys.platform)


def can_replace():
    """Whether this is a downloaded build that can replace itself."""
    return bool(getattr(sys, "frozen", False)) and asset_suffix() is not None


def _open(url, timeout, version):
    request = urllib.request.Request(url, headers={"User-Agent": "%s/%s" % (BINARY_NAME, version)})
    return urllib.request.urlopen(request, timeout=timeout)


def latest_release(current, timeout=15):
    """The newest published release. Drafts and pre-releases don't count."""
    try:
        with _open(API_URL, timeout, current) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError("GitHub answered with error %d" % e.code) from None
    return Release(data["tag_name"].lstrip("v"),
                   {a["name"]: a["browser_download_url"] for a in data.get("assets", [])})


def find_asset(release):
    """The release file for this system (with or without the version in its name)."""
    suffix = asset_suffix()
    for name in release.assets:
        if name.startswith(BINARY_NAME) and name.endswith("-" + suffix):
            return name
    raise RuntimeError("Release %s has no %s file" % (release.version, suffix))


def expected_hash(release, asset, current, timeout=30):
    """The asset's SHA-256 from the release's SHA256SUMS.txt."""
    url = release.assets.get("SHA256SUMS.txt")
    if not url:
        raise RuntimeError("Release %s has no checksums to verify the download with" % release.version)
    with _open(url, timeout, current) as resp:
        for line in resp.read().decode("utf-8").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].lstrip("*") == asset:
                return parts[0].lower()
    raise RuntimeError("The checksums of release %s don't list %s" % (release.version, asset))


def target_path(exe, current, new):
    """Where the new version goes: the same file, with the version in its name updated if it has one."""
    folder, name = os.path.split(exe)
    return os.path.join(folder, name.replace(current, new))


def clean_env():
    """Environment for starting another copy of the app as its own program. Without this, a
    one-file build would reuse the running copy's unpacked files, which go away when it exits."""
    env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")
    for key in ("TCL_LIBRARY", "TK_LIBRARY"):
        env.pop(key, None)
    return env


def _download(url, path, current, progress, timeout=60):
    """Save a URL to a file and return its SHA-256."""
    digest = hashlib.sha256()
    with _open(url, timeout, current) as resp, open(path, "wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        done = shown = 0
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)
            out.write(chunk)
            done += len(chunk)
            percent = done * 100 // total if total else 0
            if percent >= shown + 5:
                shown = percent
                progress("Downloading the update… %d%%" % percent)


def _extract_binary(archive, path):
    """Pull the app out of a release .tar.gz."""
    with tarfile.open(archive) as tar:
        for member in tar.getmembers():
            if member.isfile() and os.path.basename(member.name) == BINARY_NAME:
                with tar.extractfile(member) as src, open(path, "wb") as out:
                    while True:
                        chunk = src.read(1 << 16)
                        if not chunk:
                            return
                        out.write(chunk)
    raise RuntimeError("The download doesn't contain %s" % BINARY_NAME)


def _check_runs(path, version):
    """Run the downloaded app once, so a broken download can't replace a working app."""
    kwargs = {"creationflags": 0x08000000} if sys.platform == "win32" else {}  # CREATE_NO_WINDOW
    try:
        proc = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=120,
                              env=clean_env(), **kwargs)
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError("The downloaded update wouldn't start: %s" % e) from None
    if proc.returncode != 0 or version not in proc.stdout:
        raise RuntimeError("The downloaded update didn't start properly, so nothing was changed")


def _swap(exe, new_file, target):
    """Put new_file at target, moving the running exe aside. Puts it back if that fails."""
    aside = exe + OLD_SUFFIX
    if os.path.exists(aside):
        os.remove(aside)
    os.replace(exe, aside)
    try:
        os.replace(new_file, target)
    except OSError:
        os.replace(aside, exe)
        raise


def install(release, current, exe=None, progress=None):
    """Download a release, check it, and swap it in for the running app. Returns the new file's path."""
    say = progress or (lambda msg: None)
    exe = exe or sys.executable
    asset = find_asset(release)
    want = expected_hash(release, asset, current)
    target = target_path(exe, current, release.version)
    download = target + ".download"
    new_file = target + ".new.exe" if sys.platform == "win32" else target + ".new"  # it's run once to test it
    try:
        say("Downloading the update…")
        got = _download(release.assets[asset], download, current, say)
        if got != want:
            raise RuntimeError("The download is damaged (its checksum doesn't match), so nothing was changed")
        if asset.endswith(".tar.gz"):
            _extract_binary(download, new_file)
        else:
            os.replace(download, new_file)
        os.chmod(new_file, 0o755)
        say("Checking the update…")
        _check_runs(new_file, release.version)
        _swap(exe, new_file, target)
    finally:
        for leftover in (download, new_file):
            if os.path.exists(leftover):
                os.remove(leftover)
    return target


def relaunch(path):
    """Start the updated app, independent of this one."""
    kwargs = {} if sys.platform == "win32" else {"start_new_session": True}
    subprocess.Popen([path], env=clean_env(), cwd=os.path.dirname(path), stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)


def clean_up_old(folder=None, wait=20):
    """Remove the file an update moved aside. The old version may still be exiting, so keep trying
    for a little while in the background. Anything left is removed the next time the app starts."""
    if folder is None:
        if not can_replace():
            return
        folder = os.path.dirname(sys.executable)
    try:
        leftovers = [os.path.join(folder, n) for n in os.listdir(folder) if n.endswith(OLD_SUFFIX)]
    except OSError:
        return
    if not leftovers:
        return

    def work():
        deadline = time.monotonic() + wait
        remaining = list(leftovers)
        while remaining and time.monotonic() < deadline:
            for path in list(remaining):
                try:
                    os.remove(path)
                    remaining.remove(path)
                except OSError:
                    pass
            if remaining:
                time.sleep(1)

    threading.Thread(target=work, daemon=True).start()
