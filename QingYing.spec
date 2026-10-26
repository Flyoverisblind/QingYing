# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_dynamic_libs, collect_submodules

ROOT = Path(SPECPATH).resolve()

datas = [
    (str(ROOT / "app" / "static"), "app/static"),
    (str(ROOT / "assets"), "assets"),
]
binaries = []

# Real-ESRGAN ncnn runtime, bundled model weights and native extension.
ai_datas, ai_binaries, ai_hidden = collect_all("realesrgan_ncnn_py")
datas += ai_datas
binaries += ai_binaries

# OpenCV native libraries (the PyInstaller hook usually handles this, but being
# explicit makes the build reliable across platforms).
binaries += collect_dynamic_libs("cv2")

hiddenimports = [
    "app.main",
    "app.ai",
    "app.runner",
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    "multipart",
    "pystray",
]
hiddenimports += ai_hidden
hiddenimports += collect_submodules("pystray")

a = Analysis(
    [str(ROOT / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "pytest",
        "playwright",
        "torch",
        "onnxruntime",
        "imageio_ffmpeg",
        "matplotlib",
        "scipy",
        "pandas",
        "IPython",
        "notebook",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="QingYing",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(ROOT / "assets" / "icon.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="QingYing",
)
