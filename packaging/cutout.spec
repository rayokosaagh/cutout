# PyInstaller build for Cutout: a one-folder app in dist\Cutout (Cutout.exe + _internal).
#   venv\Scripts\pyinstaller packaging\cutout.spec --noconfirm --distpath build\dist --workpath build\work
import os
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

datas = [
    (os.path.join(ROOT, "assets", "mark-body.png"), "assets"),
    (os.path.join(ROOT, "assets", "mark-flap.png"), "assets"),
    (os.path.join(ROOT, "assets", "wordmark-mask.png"), "assets"),
    (os.path.join(ROOT, "assets", "wordmark-flap.png"), "assets"),
    (os.path.join(ROOT, "assets", "cutout.ico"), "assets"),
    (os.path.join(ROOT, "fonts"), "fonts"),
]
# numba (through pymatting) finds its cache by the source file of each jitted function,
# so pymatting ships its .py sources too.
datas += collect_data_files("pymatting", include_py_files=True)
datas += collect_data_files("rembg")
# Packages that read their own version from installed metadata at import time.
for dist in ("pymatting", "rembg", "pooch", "scikit-image", "numba", "onnxruntime", "tqdm", "jsonschema",
             "pillow-heif"):
    try:
        datas += copy_metadata(dist)
    except Exception:
        pass

a = Analysis(
    [os.path.join(ROOT, "cutout_glass.py")],
    pathex=[ROOT],
    datas=datas,
    hiddenimports=collect_submodules("rembg.sessions"),
    excludes=["tkinter", "dxcam", "comtypes", "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets",
              "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtQml",
              "PySide6.QtQuick", "PySide6.QtMultimedia", "PySide6.Qt3DCore",
              "matplotlib", "IPython", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Cutout",
    icon=os.path.join(ROOT, "assets", "cutout.ico"),
    console=False,
    version=os.path.join(SPECPATH, "version.txt"),
)
coll = COLLECT(exe, a.binaries, a.datas, name="Cutout")
