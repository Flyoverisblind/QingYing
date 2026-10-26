from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from . import config
from .ffmpeg import popen_kwargs

AI_MODELS = {
    "general": {
        "index": 4,
        "scale": 4,
        "name": "Real-ESRGAN x4plus",
        "label": "通用实拍 · x4",
        "description": "细节重建强，适合真人、风景与低清素材；显存占用较高。",
    },
    "anime": {
        "index": 2,
        "scale": 4,
        "name": "Real-ESRGAN x4plus-anime",
        "label": "动画 / 插画 · x4",
        "description": "线条干净、色块平滑，适合动画、番剧与二次元素材。",
    },
    "fast": {
        "index": 0,
        "scale": 2,
        "name": "Real-ESRGAN animevideov3 x2",
        "label": "快速 · x2",
        "description": "轻量模型，速度更快，适合 2 倍放大和预览试用。",
    },
}

_PROBE_CACHE: dict[str, Any] | None = None
_PROBE_LOCK = threading.Lock()
_PROBE_CODE = r"""
import json, sys
try:
    from realesrgan_ncnn_py import Realesrgan
    import numpy as np
    engine = Realesrgan(gpuid=0, model=0, tilesize=64)
    image = np.zeros((64, 64, 3), dtype=np.uint8)
    out = engine.process_cv2(image)
    print("QY_AI_PROBE:" + json.dumps({"available": True, "shape": list(out.shape)}))
except Exception as exc:
    print("QY_AI_PROBE:" + json.dumps({"available": False, "reason": str(exc)}))
    sys.exit(2)
"""


def package_installed() -> bool:
    return importlib.util.find_spec("realesrgan_ncnn_py") is not None


def _cache_path() -> Path:
    return config.CACHE_DIR / "ai_probe.json"


def _read_cache() -> dict[str, Any] | None:
    try:
        payload = json.loads(_cache_path().read_text(encoding="utf-8"))
        if time.time() - payload.get("created", 0) < 86400 * 7:
            return payload
    except Exception:
        pass
    return None


def _write_cache(payload: dict[str, Any]) -> None:
    try:
        _cache_path().parent.mkdir(parents=True, exist_ok=True)
        _cache_path().write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _run_probe() -> dict[str, Any]:
    if not package_installed():
        return {
            "available": False,
            "device": "",
            "reason": "未安装 AI 引擎组件（realesrgan-ncnn-py）",
            "software": False,
        }
    if config.IS_FROZEN:
        args = [sys.executable, "--ai-probe"]
        env = os.environ.copy()
    else:
        args = [sys.executable, "-c", _PROBE_CODE]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(config.APP_HOME) + os.pathsep + env.get("PYTHONPATH", "")

    env.setdefault("VK_LOADER_DEBUG", "none")
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
            env=env,
            **popen_kwargs(),
        )
    except subprocess.TimeoutExpired:
        return {
            "available": False,
            "device": "",
            "reason": "AI 引擎探测超时，显卡驱动可能不兼容 Vulkan",
            "software": False,
        }
    except Exception as exc:
        return {"available": False, "device": "", "reason": str(exc), "software": False}

    combined = f"{proc.stdout or ''}\n{proc.stderr or ''}"
    device = ""
    match = re.search(r"\[(\d+)\s+([^\]]+)\]", combined)
    if match:
        device = match.group(2).strip()
    for line in (proc.stdout or "").splitlines():
        if line.startswith("QY_AI_PROBE:"):
            try:
                payload = json.loads(line.split(":", 1)[1])
            except Exception:
                break
            payload["device"] = device
            payload["software"] = "swiftshader" in device.lower()
            if payload.get("available") and not device:
                payload["available"] = False
                payload["software"] = True
                payload["reason"] = "仅能使用 CPU 软件回退，AI 逐帧处理会极慢；请更新显卡驱动以启用 Vulkan 加速"
            elif payload.get("available") and payload["software"]:
                payload["available"] = False
                payload["reason"] = "仅检测到软件 Vulkan（性能极低），已禁用 AI 超分"
            elif not payload.get("available") and not payload.get("reason"):
                payload["reason"] = "显卡不支持所需的 Vulkan 功能"
            return payload

    reason = "未检测到可用的 Vulkan 显卡"
    if "vkEnumeratePhysicalDevices" in combined:
        reason = "未检测到可用的 Vulkan 显卡（请更新显卡驱动）"
    return {"available": False, "device": device, "reason": reason, "software": False}


def probe_ai(force: bool = False) -> dict[str, Any]:
    global _PROBE_CACHE
    with _PROBE_LOCK:
        if _PROBE_CACHE is not None and not force:
            return _PROBE_CACHE
        if not force:
            cached = _read_cache()
            if cached and cached.get("version") == config.VERSION:
                _PROBE_CACHE = cached
                return cached
        payload = _run_probe()
        payload["created"] = int(time.time())
        payload["version"] = config.VERSION
        _write_cache(payload)
        _PROBE_CACHE = payload
        return payload


def ai_status() -> dict[str, Any]:
    probe = probe_ai()
    return {
        **probe,
        "models": [
            {"value": key, **value}
            for key, value in AI_MODELS.items()
        ],
        "installed": package_installed(),
    }


def ai_available() -> bool:
    return bool(probe_ai().get("available"))


_ENGINE_CACHE: dict[str, "AIEngine"] = {}
_ENGINE_LOCK = threading.Lock()
_INFER_LOCK = threading.Lock()


class AIEngine:
    def __init__(self, model_key: str = "general", tilesize: int = 0):
        from realesrgan_ncnn_py import Realesrgan  # imported lazily

        if model_key not in AI_MODELS:
            model_key = "general"
        self.model_key = model_key
        self.info = AI_MODELS[model_key]
        self.scale = int(self.info["scale"])
        # A tile size of 0 (auto) can crash on some drivers; 128 is a safe,
        # conservative default and keeps VRAM usage low on 2 GB cards.
        tile = int(tilesize or os.environ.get("QINGYING_AI_TILE", "128"))
        self._engine = Realesrgan(
            gpuid=0,
            model=int(self.info["index"]),
            tilesize=max(32, tile),
        )

    def process_rgb(self, frame: bytes, width: int, height: int) -> bytes:
        with _INFER_LOCK:
            return self._engine.process_bytes(frame, width, height, 3)


def get_ai_engine(model_key: str = "general") -> AIEngine:
    """Keep one engine alive for the process lifetime.

    The ncnn wrapper has shutdown races on some drivers; caching avoids
    destroying the native context between jobs and saves model load time.
    """
    with _ENGINE_LOCK:
        engine = _ENGINE_CACHE.get(model_key)
        if engine is None:
            engine = AIEngine(model_key=model_key)
            _ENGINE_CACHE[model_key] = engine
        return engine


def inline_probe() -> dict[str, Any]:
    """Run the probe in the current process.  May crash if the driver is bad."""
    if not package_installed():
        return {
            "available": False,
            "device": "",
            "reason": "未安装 AI 引擎组件（realesrgan-ncnn-py）",
            "software": False,
        }
    try:
        from realesrgan_ncnn_py import Realesrgan
        import numpy as np

        engine = Realesrgan(gpuid=0, model=0, tilesize=64)
        image = np.zeros((64, 64, 3), dtype=np.uint8)
        out = engine.process_cv2(image)
        return {"available": True, "shape": list(out.shape), "reason": ""}
    except Exception as exc:
        return {"available": False, "reason": str(exc), "device": "", "software": False}


def _probe_cli_payload() -> dict[str, Any]:
    payload = inline_probe()
    if payload.get("available"):
        payload["software"] = False
    payload.setdefault("device", "")
    return payload


def ai_probe_cli() -> int:
    """Entry point used by the frozen executable's AI probe subprocess."""
    payload = _probe_cli_payload()
    line = "QY_AI_PROBE:" + json.dumps(payload, ensure_ascii=False) + "\n"
    try:
        os.write(1, line.encode("utf-8"))
    except Exception:
        print(line, end="")
    return 0 if payload.get("available") else 2
