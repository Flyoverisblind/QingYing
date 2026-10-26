from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from . import config
from .ffmpeg import (
    build_filter_chain,
    probe_media,
    popen_kwargs,
    run_capture,
)
from .schemas import JobOptions, resolve_target

_THUMB_LOCKS: dict[str, threading.Lock] = {}
_THUMB_LOCK = threading.Lock()


def _lock_for(key: str) -> threading.Lock:
    with _THUMB_LOCK:
        return _THUMB_LOCKS.setdefault(key, threading.Lock())


def list_roots() -> list[dict[str, str]]:
    roots: list[dict[str, str]] = []
    if config.IS_WINDOWS:
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            drive = f"{letter}:\\"
            if Path(drive).exists():
                roots.append({"label": drive, "path": drive})
    else:
        roots.append({"label": "/", "path": "/"})
        if config.IS_MACOS:
            volumes = Path("/Volumes")
            if volumes.exists():
                for item in sorted(volumes.iterdir()):
                    if item.is_dir():
                        roots.append({"label": item.name, "path": str(item)})
        else:
            for base in ("/mnt", "/media"):
                if Path(base).exists():
                    roots.append({"label": base, "path": base})
    home = Path.home()
    if home.exists() and all(str(home) != root["path"] for root in roots):
        roots.append({"label": "主目录", "path": str(home)})
    if config.OUTPUT_DIR.exists():
        roots.append({"label": "输出目录", "path": str(config.OUTPUT_DIR)})
    return roots


def list_directory(path: str | None) -> dict[str, Any]:
    if not path:
        path = str(Path.home())
    target = Path(path).expanduser()
    try:
        target = target.resolve(strict=True)
    except (OSError, RuntimeError):
        raise FileNotFoundError(f"目录不存在：{path}")
    if not target.is_dir():
        raise ValueError("路径不是目录")

    parent = target.parent
    if parent == target:
        parent_value = None
    else:
        parent_value = str(parent)

    directories: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    try:
        entries = list(os.scandir(target))
    except PermissionError:
        raise PermissionError(f"没有权限访问：{target}")

    for entry in entries:
        if entry.name.startswith("$"):
            continue
        try:
            if entry.is_dir(follow_symlinks=False):
                directories.append({
                    "name": entry.name,
                    "path": entry.path,
                    "is_dir": True,
                })
            elif entry.is_file(follow_symlinks=False):
                suffix = Path(entry.name).suffix.lower()
                if suffix in config.VIDEO_EXTENSIONS:
                    stat = entry.stat()
                    files.append({
                        "name": entry.name,
                        "path": entry.path,
                        "is_dir": False,
                        "size": stat.st_size,
                        "mtime": int(stat.st_mtime),
                        "ext": suffix.lstrip("."),
                    })
        except OSError:
            continue

    directories.sort(key=lambda item: item["name"].lower())
    files.sort(key=lambda item: item["name"].lower())
    return {
        "path": str(target),
        "parent": parent_value,
        "name": target.name or str(target),
        "roots": list_roots(),
        "directories": directories[:500],
        "files": files[:1000],
    }


def _thumb_key(source: Path, duration: float) -> str:
    stat = source.stat()
    raw = f"{source}|{stat.st_mtime_ns}|{stat.st_size}|{duration:.1f}|v3"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def thumbnail(source: str | Path, duration: float | None = None) -> Path:
    path = Path(source).resolve()
    if not path.exists():
        raise FileNotFoundError("视频不存在")
    if duration is None:
        try:
            duration = float(probe_media(path).get("duration") or 0)
        except Exception:
            duration = 0
    key = _thumb_key(path, duration or 0)
    out = config.CACHE_DIR / f"thumb_{key}.jpg"
    if out.exists() and out.stat().st_size > 0:
        return out
    lock = _lock_for(key)
    with lock:
        if out.exists() and out.stat().st_size > 0:
            return out
        seek = 1.0
        if duration and duration > 10:
            seek = min(max(duration * 0.08, 2.0), 90.0)
        args = [
            config.FFMPEG or "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-ss", f"{seek:.3f}", "-i", str(path), "-frames:v", "1",
            "-vf", "scale=640:-2:flags=lanczos", "-q:v", "4", "-y", str(out),
        ]
        proc = run_capture(args, timeout=120)
        if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
            raise RuntimeError("缩略图生成失败")
        return out


def _jpeg_data_url(path: Path) -> str:
    data = path.read_bytes()
    return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")


def _extract_frame(
    source: Path,
    time_seconds: float,
    *,
    width: int | None = None,
    filters: str | None = None,
    output: Path,
) -> None:
    parts: list[str] = []
    if filters:
        parts.append(filters)
    if width:
        parts.append(f"scale={width}:-2:flags=lanczos")
    filter_arg = ",".join(parts) if parts else "null"
    args = [
        config.FFMPEG or "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-ss", f"{max(0, time_seconds):.3f}", "-i", str(source),
        "-frames:v", "1", "-vf", filter_arg, "-q:v", "2", "-y", str(output),
    ]
    proc = run_capture(args, timeout=300)
    if proc.returncode != 0 or not output.exists():
        detail = (proc.stderr or "").strip().splitlines()
        raise RuntimeError(detail[-1] if detail else "预览帧生成失败")


def _ai_preview_frame(source: Path, time_seconds: float, model_key: str) -> tuple[Any, int, int]:
    import cv2  # local import keeps startup light

    from .ai import get_ai_engine

    width = height = 0
    try:
        probe = probe_media(source)
        width = int(probe["width"])
        height = int(probe["height"])
    except Exception:
        pass
    if width <= 0 or height <= 0:
        raise RuntimeError("无法读取视频尺寸")

    args = [
        config.FFMPEG or "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-ss", f"{max(0, time_seconds):.3f}", "-i", str(source),
        "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ]
    proc = subprocess.run(
        args, capture_output=True, timeout=300,
        **{k: v for k, v in popen_kwargs().items() if k != "stdin"},
    )
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError("预览帧解码失败")
    frame = proc.stdout
    engine = get_ai_engine(model_key=model_key)
    output = engine.process_rgb(frame, width, height)
    import numpy as np

    rgb = np.frombuffer(output, dtype=np.uint8).reshape(
        height * engine.scale, width * engine.scale, 3
    )
    arr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    return arr, width, height


def build_preview(path: str, time_seconds: float, options: JobOptions) -> dict[str, Any]:
    source = Path(path).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError("视频不存在")
    probe = probe_media(source)
    target = resolve_target(probe, options)
    duration = float(probe.get("duration") or 0)
    if duration > 0:
        time_seconds = min(max(0.0, time_seconds), max(0.0, duration - 0.05))

    display_width = 1280
    key_raw = json.dumps({
        "source": str(source),
        "mtime": source.stat().st_mtime_ns,
        "time": round(time_seconds, 2),
        "options": options.model_dump(),
        "target": target,
        "v": 4,
    }, sort_keys=True, ensure_ascii=False)
    key = hashlib.sha1(key_raw.encode("utf-8")).hexdigest()
    cache_dir = config.CACHE_DIR / "preview"
    cache_dir.mkdir(parents=True, exist_ok=True)
    before_path = cache_dir / f"{key}_before.jpg"
    after_path = cache_dir / f"{key}_after.jpg"
    meta_path = cache_dir / f"{key}.json"

    if before_path.exists() and after_path.exists() and meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            return {
                **meta,
                "before": _jpeg_data_url(before_path),
                "after": _jpeg_data_url(after_path),
                "cached": True,
            }
        except Exception:
            pass

    note = ""
    ai_used = False

    if not before_path.exists():
        _extract_frame(source, time_seconds, width=display_width, output=before_path)

    if options.sr_engine == "ai":
        from .ai import ai_available

        if ai_available():
            try:
                import cv2
                import numpy as np

                arr, src_w, src_h = _ai_preview_frame(source, time_seconds, options.ai_model)
                ai_used = True
                if target["width"] != arr.shape[1] or target["height"] != arr.shape[0]:
                    arr = cv2.resize(
                        arr, (target["width"], target["height"]),
                        interpolation=cv2.INTER_AREA if target["width"] < arr.shape[1] else cv2.INTER_LANCZOS4,
                    )
                if options.sharpen > 0:
                    blur = cv2.GaussianBlur(arr, (0, 0), 1.1)
                    arr = cv2.addWeighted(arr, 1 + options.sharpen * 0.35, blur, -options.sharpen * 0.35, 0)
                display_height = max(2, int(round(display_width * target["height"] / target["width"])) // 2 * 2)
                arr = cv2.resize(arr, (display_width, display_height), interpolation=cv2.INTER_AREA)
                cv2.imwrite(str(after_path), arr, [int(cv2.IMWRITE_JPEG_QUALITY), 94])
                note = "AI 预览使用单帧推理，成片会结合时域信息更稳定。"
            except Exception as exc:
                note = f"AI 预览失败，已回退到快速预览：{exc}"
        else:
            note = "当前设备未启用 AI 超分，预览展示快速超分效果。"

    if not after_path.exists():
        filters = build_filter_chain(
            denoise=options.denoise,
            source_width=int(probe["width"]),
            source_height=int(probe["height"]),
            target_width=target["width"],
            target_height=target["height"],
            sharpen=options.sharpen if options.sr_engine != "off" else 0.0,
            interp_mode="off",
        )
        _extract_frame(source, time_seconds, filters=filters, width=display_width, output=after_path)

    if options.interp != "off" and not note:
        note = "补帧效果需要播放动态画面才能体现，此处仅预览画质。"

    meta = {
        "source": str(source),
        "time": round(time_seconds, 3),
        "target": target,
        "probe": probe,
        "ai_used": ai_used,
        "note": note,
        "cached": False,
    }
    try:
        meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return {
        **meta,
        "before": _jpeg_data_url(before_path),
        "after": _jpeg_data_url(after_path),
    }


def resolve_output_path(
    source: Path,
    options: JobOptions,
    target: dict,
    *,
    job_id: str | None = None,
    preview: bool = False,
) -> Path:
    if preview:
        directory = config.PREVIEW_DIR
        base = f"preview_{job_id or 'clip'}"
        ext = ".mp4"
    else:
        directory = Path(options.output_dir).expanduser().resolve() if options.output_dir else config.OUTPUT_DIR
        directory.mkdir(parents=True, exist_ok=True)
        if options.output_name:
            base = options.output_name
            lowered = base.lower()
            for suffix in (".mp4", ".mkv", ".mov"):
                if lowered.endswith(suffix):
                    base = base[: -len(suffix)]
                    break
        else:
            height = target["height"]
            if height >= 2160:
                res_tag = "4K"
            elif height >= 1440:
                res_tag = "2K"
            elif height >= 1080:
                res_tag = "1080P"
            else:
                res_tag = f"{height}P"
            fps_tag = ""
            if options.interp != "off":
                fps = target["fps"]
                fps_tag = f"_{fps:.0f}fps" if abs(fps - round(fps)) < 0.05 else f"_{fps:.2f}fps"
            sr_tag = "_AI" if options.sr_engine == "ai" else ("_超分" if options.sr_engine == "fast" else "")
            base = f"{source.stem}{sr_tag}_{res_tag}{fps_tag}"
        ext = "." + options.container

    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / f"{base}{ext}"
    if candidate.resolve() == source.resolve():
        candidate = directory / f"{base}_enhanced{ext}"
    counter = 2
    while candidate.exists():
        candidate = directory / f"{base} ({counter}){ext}"
        counter += 1
    return candidate


def open_in_file_manager(path: str | Path) -> None:
    target = Path(path)
    if not target.exists():
        raise FileNotFoundError("路径不存在")
    if config.IS_WINDOWS:
        if target.is_file():
            subprocess.Popen(["explorer", "/select,", str(target)])
        else:
            os.startfile(str(target))  # type: ignore[attr-defined]
    elif config.IS_MACOS:
        subprocess.Popen(["open", "-R", str(target)] if target.is_file() else ["open", str(target)])
    else:
        subprocess.Popen(["xdg-open", str(target if target.is_dir() else target.parent)])


def open_external(path: str | Path) -> None:
    target = Path(path)
    if not target.exists():
        raise FileNotFoundError("文件不存在")
    if config.IS_WINDOWS:
        os.startfile(str(target))  # type: ignore[attr-defined]
    elif config.IS_MACOS:
        subprocess.Popen(["open", str(target)])
    else:
        subprocess.Popen(["xdg-open", str(target)])


def disk_space(path: Path) -> dict[str, int]:
    try:
        usage = shutil.disk_usage(path)
        return {"total": usage.total, "free": usage.free}
    except OSError:
        return {"total": 0, "free": 0}
