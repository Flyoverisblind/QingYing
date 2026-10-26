from __future__ import annotations

import os
import platform
import shutil
import sys
import tempfile
from pathlib import Path

APP_NAME = "清影"
APP_NAME_EN = "QingYing"
VERSION = "1.0.0"
DEFAULT_PORT = 17876
HOST = "127.0.0.1"

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"
IS_FROZEN = bool(getattr(sys, "frozen", False))

# Directory that holds the executable (portable build) or the repository.
APP_HOME = (
    Path(sys.executable).resolve().parent
    if IS_FROZEN
    else Path(__file__).resolve().parent.parent
)

# Read-only application resources.  PyInstaller extracts datas into _MEIPASS.
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", APP_HOME))
STATIC_DIR = RESOURCE_DIR / "app" / "static"
ASSETS_DIR = RESOURCE_DIR / "assets"


def _env_path(name: str, fallback: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw).expanduser().resolve() if raw else fallback


DATA_DIR = _env_path("QINGYING_DATA", APP_HOME / "data")
OUTPUT_DIR = _env_path("QINGYING_OUTPUT", APP_HOME / "output")
UPLOAD_DIR = DATA_DIR / "uploads"
CACHE_DIR = DATA_DIR / "cache"
PREVIEW_DIR = DATA_DIR / "previews"
LOG_DIR = DATA_DIR / "logs"
TEMP_DIR = Path(tempfile.gettempdir()) / "qingying"

PREVIEW_DIR.mkdir(parents=True, exist_ok=True)


def ensure_dirs() -> None:
    for directory in (DATA_DIR, OUTPUT_DIR, UPLOAD_DIR, CACHE_DIR, PREVIEW_DIR, LOG_DIR, TEMP_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def _find_binary(name: str, env_name: str) -> str | None:
    env_value = os.environ.get(env_name)
    if env_value and Path(env_value).is_file():
        return str(Path(env_value).resolve())

    exe = f"{name}.exe" if IS_WINDOWS else name
    candidates = [
        APP_HOME / "bin" / exe,
        RESOURCE_DIR / "bin" / exe,
        APP_HOME / "vendor" / exe,
        RESOURCE_DIR / "vendor" / exe,
    ]
    for candidate in candidates[:2]:
        if candidate.exists():
            return str(candidate)
    found = shutil.which(name)
    if found:
        return found
    for candidate in candidates[2:]:
        if candidate.exists():
            return str(candidate)
    if name == "ffmpeg":
        try:
            import imageio_ffmpeg

            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            pass
    return None


FFMPEG = _find_binary("ffmpeg", "QINGYING_FFMPEG")
FFPROBE = _find_binary("ffprobe", "QINGYING_FFPROBE")

VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".mov", ".avi", ".wmv", ".flv", ".webm", ".m4v",
    ".ts", ".mts", ".m2ts", ".mpg", ".mpeg", ".vob", ".3gp", ".ogv",
    ".rmvb", ".rm", ".asf", ".f4v", ".divx",
}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}

MAX_UPLOAD_BYTES = int(os.environ.get("QINGYING_MAX_UPLOAD_GB", "20")) * 1024**3
MAX_JOBS_KEPT = 60

SYSTEM_INFO = {
    "platform": platform.system(),
    "release": platform.release(),
    "machine": platform.machine(),
    "python": platform.python_version(),
}
