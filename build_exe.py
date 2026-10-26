"""Build the portable QingYing desktop folder and zip archive.

Usage:  python build_exe.py
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist" / "QingYing"


def find_ffmpeg() -> Path | None:
    bundled = ROOT / "vendor" / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    if bundled.exists():
        return bundled
    try:
        import imageio_ffmpeg

        return Path(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:
        return None


def main() -> int:
    print("== 清影 QingYing build ==")
    if DIST.exists():
        shutil.rmtree(DIST)
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "QingYing.spec"],
        cwd=ROOT,
        check=True,
    )
    if not DIST.exists():
        print(f"Build output not found: {DIST}")
        return 1

    bin_dir = DIST / "bin"
    bin_dir.mkdir(exist_ok=True)
    ffmpeg = find_ffmpeg()
    if ffmpeg and ffmpeg.exists():
        target_name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
        shutil.copy2(ffmpeg, bin_dir / target_name)
        print(f"Bundled FFmpeg: {ffmpeg} -> {bin_dir / target_name}")
    else:
        print("WARNING: FFmpeg not found; the app will look for it on PATH.")

    for name in ("README.md", "LICENSE"):
        source = ROOT / name
        if source.exists():
            shutil.copy2(source, DIST / name)
    shots = ROOT / "screenshots"
    if shots.exists():
        target_shots = DIST / "screenshots"
        if target_shots.exists():
            shutil.rmtree(target_shots)
        shutil.copytree(shots, target_shots)

    system = platform.system().lower()
    archive_base = ROOT / "dist" / f"QingYing-{system}-{platform.machine().lower()}"
    if archive_base.with_suffix(".zip").exists():
        archive_base.with_suffix(".zip").unlink()
    print("Packing zip (this may take a minute)…")
    archive = shutil.make_archive(str(archive_base), "zip", root_dir=DIST.parent, base_dir=DIST.name)
    size_mb = Path(archive).stat().st_size / 1024**2
    print(f"\nPortable app: {DIST}")
    print(f"Archive:      {archive} ({size_mb:.0f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
