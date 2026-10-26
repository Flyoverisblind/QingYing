from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import config

_CREATE_NO_WINDOW = 0x08000000 if config.IS_WINDOWS else 0


def popen_kwargs() -> dict[str, Any]:
    """Common subprocess options: never flash a console window on Windows."""
    kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL}
    if config.IS_WINDOWS:
        kwargs["creationflags"] = _CREATE_NO_WINDOW
    return kwargs


def run_capture(args: Iterable[str], timeout: float = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        **popen_kwargs(),
    )


def binary_ok(path: str | None) -> bool:
    return bool(path and Path(path).is_file())


def ffmpeg_version() -> str:
    if not config.FFMPEG:
        return "未找到"
    try:
        proc = run_capture([config.FFMPEG, "-version"], timeout=15)
        first = (proc.stdout or proc.stderr).splitlines()[0]
        match = re.search(r"ffmpeg version\s+(\S+)", first)
        return match.group(1) if match else first[:60]
    except Exception:
        return "未知"


def _fraction(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        if "/" in value:
            numerator, denominator = value.split("/", 1)
            denominator_f = float(denominator)
            return float(numerator) / denominator_f if denominator_f else 0.0
        return float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def _format_duration(seconds: float) -> str:
    seconds = max(0, seconds)
    hours, rem = divmod(int(seconds), 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{int((seconds % 1) * 1000):03d}"


def _parse_ffmpeg_stderr(stderr: str, path: Path, size: int) -> dict[str, Any]:
    duration = 0.0
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", stderr)
    if match:
        duration = int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))

    video_line = next((line for line in stderr.splitlines() if "Video:" in line), "")
    audio_lines = [line for line in stderr.splitlines() if "Audio:" in line]
    width = height = 0
    dim_match = re.search(r",\s*(\d{2,5})x(\d{2,5})[\s,\[]", video_line)
    if dim_match:
        width, height = int(dim_match.group(1)), int(dim_match.group(2))

    fps = 0.0
    fps_match = re.search(r"(\d+(?:\.\d+)?)\s+fps", video_line)
    if fps_match:
        fps = float(fps_match.group(1))
    else:
        tbr_match = re.search(r"(\d+(?:\.\d+)?)\s+tbr", video_line)
        if tbr_match:
            fps = float(tbr_match.group(1))

    codec = ""
    codec_match = re.search(r"Video:\s*([a-zA-Z0-9_]+)", video_line)
    if codec_match:
        codec = codec_match.group(1)
    audio_codec = ""
    if audio_lines:
        audio_match = re.search(r"Audio:\s*([a-zA-Z0-9_]+)", audio_lines[0])
        if audio_match:
            audio_codec = audio_match.group(1)

    bitrate = 0
    br_match = re.search(r"bitrate:\s*(\d+)\s*kb/s", stderr)
    if br_match:
        bitrate = int(br_match.group(1)) * 1000

    rotation = 0
    rot_match = re.search(r"rotate\s*:\s*(-?\d+)", stderr)
    if rot_match:
        rotation = int(rot_match.group(1)) % 360
    matrix_match = re.search(
        r"displaymatrix:\s*rotation of\s*(-?\d+(?:\.\d+)?)\s*degrees", stderr, re.I
    )
    if matrix_match and not rotation:
        rotation = int(round(float(matrix_match.group(1)))) % 360

    hdr = any(token in video_line.lower() for token in ("smpte2084", "arib-std-b67", "bt2020"))
    display_width, display_height = width, height
    if rotation in (90, 270):
        display_width, display_height = height, width

    return {
        "path": str(path),
        "name": path.name,
        "size": size or path.stat().st_size,
        "duration": round(duration, 3),
        "width": display_width,
        "height": display_height,
        "coded_width": width,
        "coded_height": height,
        "fps": round(fps, 4),
        "video_codec": codec,
        "audio_codec": audio_codec,
        "has_audio": bool(audio_lines),
        "bitrate": bitrate,
        "rotation": rotation,
        "hdr": hdr,
        "pix_fmt": "",
        "format": path.suffix.lstrip(".").lower(),
        "probe_engine": "ffmpeg",
    }


def _parse_ffprobe_json(data: dict[str, Any], path: Path, size: int) -> dict[str, Any]:
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not video:
        raise ValueError("文件中没有找到视频流")
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    fmt = data.get("format") or {}

    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)

    rotation = 0
    for side_data in video.get("side_data_list") or []:
        if "rotation" in side_data:
            try:
                rotation = int(float(side_data["rotation"])) % 360
            except (TypeError, ValueError):
                rotation = 0
    if not rotation:
        try:
            rotation = int(float((video.get("tags") or {}).get("rotate", 0))) % 360
        except (TypeError, ValueError):
            rotation = 0

    display_width, display_height = width, height
    if rotation in (90, 270):
        display_width, display_height = height, width

    fps = _fraction(video.get("avg_frame_rate")) or _fraction(video.get("r_frame_rate"))
    duration = 0.0
    for raw in (fmt.get("duration"), video.get("duration")):
        try:
            duration = float(raw)
            if duration > 0:
                break
        except (TypeError, ValueError):
            continue

    try:
        bitrate = int(fmt.get("bit_rate") or video.get("bit_rate") or 0)
    except (TypeError, ValueError):
        bitrate = 0

    color_transfer = (video.get("color_transfer") or "").lower()
    hdr = color_transfer in {"smpte2084", "arib-std-b67"} or "bt2020" in (
        (video.get("color_primaries") or "").lower()
    )

    audio_codec = audio_streams[0].get("codec_name", "") if audio_streams else ""
    return {
        "path": str(path),
        "name": path.name,
        "size": size or path.stat().st_size,
        "duration": round(duration, 3),
        "width": display_width,
        "height": display_height,
        "coded_width": width,
        "coded_height": height,
        "fps": round(fps, 4),
        "video_codec": video.get("codec_name", ""),
        "audio_codec": audio_codec,
        "has_audio": bool(audio_streams),
        "audio_streams": len(audio_streams),
        "bitrate": bitrate,
        "rotation": rotation,
        "hdr": hdr,
        "pix_fmt": video.get("pix_fmt", ""),
        "color_space": video.get("color_space", ""),
        "color_transfer": video.get("color_transfer", ""),
        "color_primaries": video.get("color_primaries", ""),
        "format": (fmt.get("format_name") or path.suffix.lstrip(".")).split(",")[0],
        "probe_engine": "ffprobe",
    }


def probe_media(path: str | Path) -> dict[str, Any]:
    file_path = Path(path).expanduser().resolve()
    if not file_path.exists():
        raise FileNotFoundError(f"文件不存在：{file_path}")
    if not file_path.is_file():
        raise ValueError("所选路径不是文件")
    size = file_path.stat().st_size
    if size <= 0:
        raise ValueError("文件为空")

    if binary_ok(config.FFPROBE):
        args = [
            config.FFPROBE, "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", str(file_path),
        ]
        try:
            proc = run_capture(args, timeout=90)
            if proc.returncode == 0:
                data = json.loads(proc.stdout or "{}")
                return _parse_ffprobe_json(data, file_path, size)
        except Exception:
            pass

    if not binary_ok(config.FFMPEG):
        raise RuntimeError("未找到 FFmpeg，请先安装或配置 QINGYING_FFMPEG")
    proc = run_capture([config.FFMPEG, "-hide_banner", "-i", str(file_path)], timeout=90)
    stderr = proc.stderr or ""
    if "Video:" not in stderr:
        detail = stderr.strip().splitlines()[-1] if stderr.strip() else "无法读取视频信息"
        raise ValueError(f"无法识别视频文件：{detail}")
    return _parse_ffmpeg_stderr(stderr, file_path, size)


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

DENOISE_PRESETS = {
    "off": None,
    "light": "1.2:1.2:4:4",
    "medium": "2.2:2.2:7:7",
    "strong": "3.5:3.5:11:11",
}

INTERP_MODES = {
    "smooth": "minterpolate=fps={fps}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1:search_param=32:scd=fdiff:scd_threshold=10",
    "balanced": "minterpolate=fps={fps}:mi_mode=mci:mc_mode=aobmc:me_mode=bilat:vsbmc=0:search_param=24:scd=fdiff:scd_threshold=10",
    "fast": "minterpolate=fps={fps}:mi_mode=blend",
}


def build_filter_chain(
    *,
    denoise: str = "off",
    source_width: int,
    source_height: int,
    target_width: int,
    target_height: int,
    sharpen: float = 0.0,
    interp_mode: str = "off",
    target_fps: float | None = None,
    ai_input: bool = False,
    order: str = "interp_first",
) -> str:
    denoise_filter = ""
    preset = DENOISE_PRESETS.get(denoise)
    if preset and not ai_input:
        denoise_filter = f"hqdn3d={preset}"

    need_scale = target_width != source_width or target_height != source_height or not ai_input
    scale_filter = (
        f"scale={target_width}:{target_height}:flags=lanczos+accurate_rnd+full_chroma_int"
        if need_scale
        else ""
    )
    sharpen_filter = ""
    if sharpen and sharpen > 0:
        amount = max(0.05, min(1.5, float(sharpen)))
        sharpen_filter = f"unsharp=5:5:{amount:.2f}:3:3:0"
    interp_filter = ""
    if interp_mode in INTERP_MODES and target_fps:
        fps_text = f"{target_fps:.5f}".rstrip("0").rstrip(".")
        interp_filter = INTERP_MODES[interp_mode].format(fps=fps_text)

    filters: list[str] = []
    if ai_input:
        # AI already produced high-resolution frames: scale, then interpolate.
        filters = [item for item in (scale_filter, sharpen_filter, interp_filter) if item]
    elif interp_filter and order == "interp_first":
        # Interpolating at source resolution is dramatically faster and is the
        # practical default for long videos.  Detail is restored by the scaler.
        filters = [item for item in (denoise_filter, interp_filter, scale_filter, sharpen_filter) if item]
    else:
        filters = [item for item in (denoise_filter, scale_filter, sharpen_filter, interp_filter) if item]

    filters.append("setsar=1")
    return ",".join(filters)


# ---------------------------------------------------------------------------
# Encoders
# ---------------------------------------------------------------------------

SOFTWARE_ENCODERS = {
    "h264": "libx264",
    "h265": "libx265",
    "av1": "libsvtav1",
}

HARDWARE_ENCODERS = {
    "h264": ["h264_nvenc", "h264_qsv", "h264_amf", "h264_videotoolbox"],
    "h265": ["hevc_nvenc", "hevc_qsv", "hevc_amf", "hevc_videotoolbox"],
    "av1": ["av1_nvenc", "av1_qsv", "av1_amf"],
}

ENCODER_LABELS = {
    "libx264": "H.264 · CPU (x264)",
    "libx265": "H.265 · CPU (x265)",
    "libsvtav1": "AV1 · CPU (SVT-AV1)",
    "h264_nvenc": "H.264 · NVIDIA NVENC",
    "hevc_nvenc": "H.265 · NVIDIA NVENC",
    "av1_nvenc": "AV1 · NVIDIA NVENC",
    "h264_qsv": "H.264 · Intel Quick Sync",
    "hevc_qsv": "H.265 · Intel Quick Sync",
    "av1_qsv": "AV1 · Intel Quick Sync",
    "h264_amf": "H.264 · AMD AMF",
    "hevc_amf": "H.265 · AMD AMF",
    "av1_amf": "AV1 · AMD AMF",
    "h264_videotoolbox": "H.264 · Apple VideoToolbox",
    "hevc_videotoolbox": "H.265 · Apple VideoToolbox",
}

QUALITY_CRF = {
    "h264": {"high": 17, "balanced": 20, "small": 23},
    "h265": {"high": 19, "balanced": 22, "small": 25},
    "av1": {"high": 27, "balanced": 31, "small": 35},
}

_ENCODER_CACHE: dict[str, Any] | None = None
_ENCODER_LOCK = threading.Lock()


def _encoder_cache_path() -> Path:
    return config.CACHE_DIR / "encoders.json"


def list_ffmpeg_encoders() -> set[str]:
    if not binary_ok(config.FFMPEG):
        return set()
    try:
        proc = run_capture([config.FFMPEG, "-hide_banner", "-encoders"], timeout=30)
        text = proc.stdout or ""
        return set(re.findall(r"^\s*V\S*\s+(\S+)", text, flags=re.MULTILINE))
    except Exception:
        return set()


def _test_encoder(encoder: str, ffmpeg_path: str) -> bool:
    args = [
        ffmpeg_path, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
        "-i", "color=c=black:s=128x128:r=1:d=0.2", "-frames:v", "1",
        "-pix_fmt", "yuv420p", "-c:v", encoder, "-f", "null", "-",
    ]
    try:
        proc = run_capture(args, timeout=20)
        return proc.returncode == 0
    except Exception:
        return False


def detect_encoders(force: bool = False) -> dict[str, Any]:
    global _ENCODER_CACHE
    with _ENCODER_LOCK:
        if _ENCODER_CACHE is not None and not force:
            return _ENCODER_CACHE

        cache_path = _encoder_cache_path()
        ffmpeg_path = config.FFMPEG or ""
        if not force and cache_path.exists():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                if (
                    cached.get("ffmpeg") == ffmpeg_path
                    and time.time() - cached.get("created", 0) < 86400 * 7
                    and time.time() - cached.get("created", 0) > 0
                ):
                    _ENCODER_CACHE = cached
                    return cached
            except Exception:
                pass

        available = list_ffmpeg_encoders()
        hardware: dict[str, str] = {}
        if ffmpeg_path:
            for codec, candidates in HARDWARE_ENCODERS.items():
                for candidate in candidates:
                    if candidate in available and _test_encoder(candidate, ffmpeg_path):
                        hardware[codec] = candidate
                        break

        payload = {
            "ffmpeg": ffmpeg_path,
            "created": int(time.time()),
            "available": sorted(available),
            "hardware": hardware,
            "software": SOFTWARE_ENCODERS,
        }
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
        _ENCODER_CACHE = payload
        return payload


def codec_options() -> list[dict[str, Any]]:
    encoders = detect_encoders()
    hardware = encoders.get("hardware", {})
    available = set(encoders.get("available") or [])
    options: list[dict[str, Any]] = []
    for codec in ("h264", "h265", "av1"):
        software = SOFTWARE_ENCODERS[codec]
        if available and software not in available:
            continue
        if not available and codec != "h264":
            continue
        options.append({
            "value": software,
            "codec": codec,
            "label": ENCODER_LABELS.get(software, software),
            "hardware": False,
        })
        if codec in hardware:
            hw = hardware[codec]
            options.append({
                "value": hw,
                "codec": codec,
                "label": ENCODER_LABELS.get(hw, hw),
                "hardware": True,
            })
    return options


def resolve_encoder(codec: str, encoder: str | None = None) -> str:
    if encoder:
        valid = {item["value"] for item in codec_options()}
        if encoder in valid:
            return encoder
        if encoder in SOFTWARE_ENCODERS.values():
            return encoder
    return SOFTWARE_ENCODERS.get(codec, "libx264")


def encoder_args(
    *,
    encoder: str,
    quality: str = "balanced",
    crf: int | None = None,
    preset: str = "medium",
) -> list[str]:
    if encoder == "libx264":
        value = crf if crf is not None else QUALITY_CRF["h264"].get(quality, 20)
        return ["-c:v", "libx264", "-preset", preset, "-crf", str(value),
                "-profile:v", "high", "-pix_fmt", "yuv420p"]
    if encoder == "libx265":
        value = crf if crf is not None else QUALITY_CRF["h265"].get(quality, 22)
        return ["-c:v", "libx265", "-preset", preset, "-crf", str(value),
                "-pix_fmt", "yuv420p", "-x265-params", "log-level=error"]
    if encoder == "libsvtav1":
        value = crf if crf is not None else QUALITY_CRF["av1"].get(quality, 31)
        av1_preset = "6" if preset == "medium" else ("4" if preset == "slow" else "9")
        return ["-c:v", "libsvtav1", "-preset", av1_preset, "-crf", str(value), "-pix_fmt", "yuv420p"]
    if encoder.endswith("_nvenc"):
        base = encoder.split("_")[0]
        value = crf if crf is not None else QUALITY_CRF.get(base, {}).get(quality, 21)
        nv_preset = {"slow": "p7", "medium": "p5", "fast": "p3"}.get(preset, "p5")
        return ["-c:v", encoder, "-preset", nv_preset, "-tune", "hq",
                "-rc", "vbr", "-cq", str(value), "-b:v", "0", "-pix_fmt", "yuv420p"]
    if encoder.endswith("_qsv"):
        base = encoder.split("_")[0]
        value = crf if crf is not None else QUALITY_CRF.get(base, {}).get(quality, 21)
        return ["-c:v", encoder, "-preset", "medium", "-global_quality", str(value),
                "-pix_fmt", "yuv420p"]
    if encoder.endswith("_amf"):
        base = encoder.split("_")[0]
        value = crf if crf is not None else QUALITY_CRF.get(base, {}).get(quality, 21)
        return ["-c:v", encoder, "-quality", "quality", "-rc", "cqp",
                "-qp_i", str(value), "-qp_p", str(value), "-pix_fmt", "yuv420p"]
    if encoder.endswith("_videotoolbox"):
        base = encoder.split("_")[0]
        value = crf if crf is not None else QUALITY_CRF.get(base, {}).get(quality, 21)
        return ["-c:v", encoder, "-q:v", str(value), "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p"]


def audio_args(mode: str, container: str, audio_codec: str = "") -> list[str]:
    if mode == "none":
        return ["-an"]
    if mode == "aac":
        return ["-c:a", "aac", "-b:a", "192k"]
    if container == "mp4" and audio_codec.lower() not in {
        "aac", "mp3", "ac3", "eac3", "alac", "opus",
    }:
        return ["-c:a", "aac", "-b:a", "192k"]
    return ["-c:a", "copy"]


def container_args(container: str) -> list[str]:
    args = ["-max_muxing_queue_size", "2048", "-map_chapters", "-1"]
    if container == "mp4":
        args += ["-movflags", "+faststart"]
    return args


def format_time(seconds: float) -> str:
    return _format_duration(seconds)


def check_binaries() -> dict[str, Any]:
    return {
        "ffmpeg": config.FFMPEG,
        "ffprobe": config.FFPROBE,
        "ffmpeg_ok": binary_ok(config.FFMPEG),
        "ffprobe_ok": binary_ok(config.FFPROBE),
        "ffmpeg_version": ffmpeg_version(),
    }
