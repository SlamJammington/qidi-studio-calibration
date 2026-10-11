# PyInstaller build recipe. Used by .github/workflows/release.yml; to build locally:
#   pip install pyinstaller
#   pyinstaller qidi_studio_calibration.spec
# Output: dist/QidiStudioCalibration.exe (Windows), dist/QidiStudioCalibration.app (macOS), dist/QidiStudioCalibration (Linux)
import re
import sys

version = re.search(r'__version__ = "([^"]+)"', open("qidi_studio_calibration.py", encoding="utf-8").read()).group(1)

a = Analysis(
    ["qidi_studio_calibration.py"],
    datas=[("assets/digits.json", "assets"), ("assets/icon.png", "assets"),
           ("LICENSE", "."), ("assets/LICENSE-DejaVu-font.txt", "assets")],
    excludes=["unittest", "pydoc", "doctest", "lib2to3", "pdb"],
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name="QidiStudioCalibration",
    console=False,  # GUI app; qidi_studio_calibration.attach_console() handles command-line use on Windows
    icon="assets/icon.icns" if sys.platform == "darwin" else "assets/icon.ico",
    upx=False,      # UPX-packed executables trip antivirus heuristics far more often
)

if sys.platform == "darwin":
    app = BUNDLE(
        exe,
        name="QidiStudioCalibration.app",
        icon="assets/icon.icns",
        bundle_identifier="io.github.slamjammington.qidistudiocalibration",
        version=version,
        info_plist={"NSHighResolutionCapable": True, "CFBundleDisplayName": "QIDI Studio Calibration"},
    )
