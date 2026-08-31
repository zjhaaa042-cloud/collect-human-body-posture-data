# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_dynamic_libs


ROOT = Path(SPEC).resolve().parent.parent

datas = []
binaries = []
hiddenimports = []

for package in ("pyorbbecsdk", "pyrealsense2", "vosk"):
    try:
        package_datas, package_binaries, package_hiddenimports = collect_all(package)
    except Exception:
        continue
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hiddenimports

# pygame imports pkg_resources, whose Python 3.10 setuptools vendor path needs
# these modules to be present even though imports use the top-level alias.
hiddenimports += [
    "setuptools._vendor.backports",
    "setuptools._vendor.backports.tarfile",
]

try:
    binaries += collect_dynamic_libs("cv2")
except Exception:
    pass

a = Analysis(
    [str(ROOT / "run_backend.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "IPython", "matplotlib", "notebook", "tkinter.test"],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="body-posture-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    # Electron starts this process with windowsHide=true. Keeping a console
    # stream prevents sys.stdout/sys.stderr from becoming None in PyInstaller.
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    icon=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="body-posture-backend",
)
