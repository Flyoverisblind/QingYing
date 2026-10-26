from __future__ import annotations

import re
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

from . import config
from .ffmpeg import (
    audio_args,
    build_filter_chain,
    container_args,
    encoder_args,
    popen_kwargs,
    probe_media,
    resolve_encoder,
)
from .schemas import JobOptions, resolve_target

_ERROR_HINTS = [
    (re.compile(r"No space left on device", re.I), "磁盘空间不足"),
    (re.compile(r"Unknown encoder", re.I), "当前 FFmpeg 不支持所选编码器"),
    (re.compile(r"Cannot allocate memory|out of memory", re.I), "内存不足，请降低分辨率或关闭其他程序"),
    (re.compile(r"Invalid data found", re.I), "视频文件损坏或格式不受支持"),
    (re.compile(r"Permission denied", re.I), "没有写入权限，请更换输出目录"),
    (re.compile(r"No such file or directory", re.I), "文件或目录不存在"),
]


def _terminate(processes: list[subprocess.Popen]) -> None:
    for process in processes:
        if process.poll() is None:
            try:
                process.terminate()
            except Exception:
                pass
    deadline = time.time() + 3
    for process in processes:
        if process.poll() is None:
            try:
                process.wait(timeout=max(0.1, deadline - time.time()))
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass


def _drain_stderr(process: subprocess.Popen, lines: deque[str], on_line: Callable[[str], None] | None = None) -> None:
    if not process.stderr:
        return
    try:
        for line in iter(process.stderr.readline, ""):
            if not line:
                break
            if isinstance(line, bytes):
                text = line.decode("utf-8", errors="replace").rstrip()
            else:
                text = line.rstrip()
            if text:
                lines.append(text)
                if on_line:
                    on_line(text)
    except Exception:
        pass


def _friendly_error(lines: deque[str], returncode: int | None) -> str:
    tail = "\n".join(list(lines)[-25:])
    for pattern, message in _ERROR_HINTS:
        if pattern.search(tail):
            return message
    for line in reversed(list(lines)):
        stripped = line.strip()
        if stripped and not stripped.startswith(("frame=", "video:", "Stream mapping", "Press [q]")):
            return stripped[-300:]
    return f"FFmpeg 处理失败（退出码 {returncode}）"


def _parse_progress(line: str, state: dict[str, Any]) -> bool:
    if "=" not in line:
        return False
    key, value = line.strip().split("=", 1)
    state[key] = value
    return key == "progress"


def _report_ffmpeg_progress(
    job: Any,
    state: dict[str, Any],
    total_duration: float,
    last_report: list[float],
) -> None:
    now = time.time()
    if now - last_report[0] < 0.35 and state.get("progress") != "end":
        return
    last_report[0] = now
    seconds = 0.0
    for key in ("out_time_us", "out_time_ms"):
        raw = state.get(key)
        if raw and raw not in {"N/A", "0"}:
            try:
                seconds = float(raw) / 1_000_000.0
                break
            except ValueError:
                pass
    if not seconds:
        raw = state.get("out_time")
        if raw and raw != "N/A":
            match = re.match(r"(\d+):(\d+):(\d+(?:\.\d+)?)", raw)
            if match:
                seconds = int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))
    speed = None
    raw_speed = state.get("speed")
    if raw_speed:
        match = re.match(r"([\d.]+)x", raw_speed)
        if match:
            try:
                speed = float(match.group(1))
            except ValueError:
                speed = None
    fps = None
    try:
        fps = float(state.get("fps")) if state.get("fps") not in (None, "N/A") else None
    except ValueError:
        fps = None
    progress = 0.0
    if total_duration > 0:
        progress = min(99.0, max(0.0, seconds / total_duration * 100))
    eta = None
    if speed and total_duration > 0 and seconds < total_duration:
        eta = (total_duration - seconds) / speed
    job.update(
        progress=round(progress, 2),
        processed_seconds=round(seconds, 2),
        speed=round(speed, 2) if speed else None,
        fps=round(fps, 2) if fps else None,
        eta_seconds=round(eta, 1) if eta else None,
    )


def _part_path(output: Path) -> Path:
    return output.with_name(f"{output.stem}.part{output.suffix}")


def _encode_args(options: JobOptions) -> list[str]:
    encoder = resolve_encoder(options.codec, options.encoder)
    args = encoder_args(
        encoder=encoder,
        quality=options.quality,
        crf=options.crf,
        preset=options.preset,
    )
    if encoder in {"libx265", "hevc_nvenc", "hevc_qsv", "hevc_amf"} and options.container == "mp4":
        args += ["-tag:v", "hvc1"]
    return args


def _validate_output(path: Path, expected_duration: float) -> None:
    try:
        info = probe_media(path)
    except Exception:
        return
    actual = float(info.get("duration") or 0)
    if expected_duration > 1.0 and actual > 0 and actual < expected_duration * 0.85:
        raise RuntimeError(
            f"输出时长异常（{actual:.1f}s，预期约 {expected_duration:.1f}s），"
            "编码可能被中断，请重试"
        )


def run_filter_job(job: Any, probe: dict[str, Any], target: dict[str, Any]) -> Path:
    options: JobOptions = job.options
    source = Path(job.source)
    output = job.output_path
    part = _part_path(output)
    duration = target["end"] - target["start"] if target["end"] is not None else target["duration"] - target["start"]
    if duration <= 0:
        raise ValueError("处理时长无效")

    filters = build_filter_chain(
        denoise=options.denoise,
        source_width=target["source_width"],
        source_height=target["source_height"],
        target_width=target["width"],
        target_height=target["height"],
        sharpen=options.sharpen if options.sr_engine != "off" else 0.0,
        interp_mode=options.interp,
        target_fps=target["fps"] if options.interp != "off" else None,
        order=options.order,
    )

    args: list[str] = [config.FFMPEG or "ffmpeg", "-hide_banner", "-y", "-nostdin"]
    args += ["-ss", f"{target['start']:.3f}", "-i", str(source), "-t", f"{duration:.3f}"]
    args += ["-map", "0:v:0", "-map", "0:a?", "-sn", "-dn", "-map_metadata", "0"]
    args += ["-metadata:s:v:0", "rotate=0"]
    args += ["-vf", filters]
    args += _encode_args(options)
    args += audio_args(options.audio, options.container, probe.get("audio_codec", ""))
    args += container_args(options.container)
    args += ["-progress", "pipe:1", "-nostats", str(part)]

    if part.exists():
        part.unlink()
    job.add_log(f"执行: {' '.join(args)}")
    job.update(stage="启动编码器", message="正在准备处理管线…", eta_seconds=None)
    process = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        **popen_kwargs(),
    )
    job.processes.append(process)
    stderr_lines: deque[str] = deque(maxlen=200)
    job.stderr_lines = stderr_lines
    stderr_thread = threading.Thread(
        target=_drain_stderr,
        args=(process, stderr_lines, job.add_log),
        daemon=True,
    )
    stderr_thread.start()

    state: dict[str, Any] = {}
    last_report = [0.0]
    stage_label = "超分 + 补帧" if options.sr_engine != "off" and options.interp != "off" else (
        "AI 超分" if options.sr_engine == "ai" else ("超分处理" if options.sr_engine == "fast" else "补帧处理")
    )
    job.update(stage=stage_label)

    assert process.stdout is not None
    try:
        for line in process.stdout:
            if job.cancel_event.is_set():
                _terminate([process])
                break
            if _parse_progress(line.rstrip(), state):
                _report_ffmpeg_progress(job, state, duration, last_report)
        returncode = process.wait(timeout=15)
    except Exception:
        _terminate([process])
        raise
    finally:
        stderr_thread.join(timeout=2)
        _terminate([process])

    if job.cancel_event.is_set():
        if part.exists():
            part.unlink(missing_ok=True)
        raise RuntimeError("CANCELED")
    if returncode != 0:
        if part.exists():
            part.unlink(missing_ok=True)
        raise RuntimeError(_friendly_error(stderr_lines, returncode))
    if not part.exists() or part.stat().st_size == 0:
        raise RuntimeError("没有生成有效的输出文件，请检查源视频与编码设置")

    try:
        _validate_output(part, duration)
    except Exception:
        part.unlink(missing_ok=True)
        raise
    part.replace(output)
    job.update(progress=100.0, processed_seconds=round(duration, 2))
    return output


def _read_exact(stream: Any, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def run_ai_job(job: Any, probe: dict[str, Any], target: dict[str, Any]) -> Path:
    from .ai import ai_available, get_ai_engine

    if not ai_available():
        raise RuntimeError("AI 超分引擎不可用，请改用快速超分或检查显卡驱动")

    options: JobOptions = job.options
    source = Path(job.source)
    output = job.output_path
    part = _part_path(output)
    duration = target["end"] - target["start"] if target["end"] is not None else target["duration"] - target["start"]
    if duration <= 0:
        raise ValueError("处理时长无效")

    source_width = int(probe["width"])
    source_height = int(probe["height"])
    source_fps = float(probe.get("fps") or 0)
    if source_fps <= 0:
        raise RuntimeError("无法读取源视频帧率")

    job.update(stage="加载 AI 模型", message="正在初始化 Real-ESRGAN…")
    engine = get_ai_engine(options.ai_model)
    ai_width = source_width * engine.scale
    ai_height = source_height * engine.scale

    denoise_filter = ""
    if options.denoise != "off":
        from .ffmpeg import DENOISE_PRESETS

        preset = DENOISE_PRESETS.get(options.denoise)
        if preset:
            denoise_filter = f"hqdn3d={preset}"

    decode_args: list[str] = [config.FFMPEG or "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
    decode_args += ["-ss", f"{target['start']:.3f}", "-i", str(source), "-t", f"{duration:.3f}"]
    decode_args += ["-map", "0:v:0", "-an", "-sn", "-dn"]
    if denoise_filter:
        decode_args += ["-vf", denoise_filter]
    decode_args += ["-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    job.add_log(f"解码: {' '.join(decode_args)}")

    post_filters = build_filter_chain(
        denoise="off",
        source_width=ai_width,
        source_height=ai_height,
        target_width=target["width"],
        target_height=target["height"],
        sharpen=options.sharpen if options.sr_engine != "off" else 0.0,
        interp_mode=options.interp,
        target_fps=target["fps"] if options.interp != "off" else None,
        ai_input=True,
    )

    encode_args: list[str] = [config.FFMPEG or "ffmpeg", "-hide_banner", "-y", "-nostdin"]
    encode_args += ["-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{ai_width}x{ai_height}",
                    "-r", f"{source_fps:.6f}", "-i", "-"]
    encode_args += ["-ss", f"{target['start']:.3f}", "-t", f"{duration:.3f}", "-i", str(source)]
    encode_args += ["-map", "0:v:0", "-map", "1:a?", "-sn", "-dn", "-map_metadata", "1"]
    encode_args += ["-metadata:s:v:0", "rotate=0"]
    encode_args += ["-vf", post_filters]
    encode_args += _encode_args(options)
    encode_args += audio_args(options.audio, options.container, probe.get("audio_codec", ""))
    encode_args += container_args(options.container)
    encode_args += ["-progress", "pipe:1", "-nostats", str(part)]
    job.add_log(f"编码: {' '.join(encode_args)}")

    if part.exists():
        part.unlink()

    decoder = subprocess.Popen(
        decode_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        **popen_kwargs(),
    )
    encoder = subprocess.Popen(
        encode_args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=False, bufsize=0, **{k: v for k, v in popen_kwargs().items() if k != "stdin"},
    )
    job.processes.extend([decoder, encoder])
    decode_errors: deque[str] = deque(maxlen=100)
    encode_errors: deque[str] = deque(maxlen=200)
    job.stderr_lines = encode_errors
    threading.Thread(target=_drain_stderr, args=(decoder, decode_errors), daemon=True).start()
    threading.Thread(target=_drain_stderr, args=(encoder, encode_errors, job.add_log), daemon=True).start()

    frame_size = source_width * source_height * 3
    total_frames = max(1, int(round(duration * source_fps)))
    processed = 0
    stage_label = "AI 超分 + 补帧" if options.interp != "off" else "AI 超分处理"
    job.update(stage=stage_label, message="逐帧 AI 重建中…", eta_seconds=None)

    encode_state: dict[str, Any] = {}
    last_report = [0.0]
    stdout_thread_error: list[Exception] = []

    def read_encoder_progress() -> None:
        assert encoder.stdout is not None
        try:
            for line in iter(encoder.stdout.readline, b""):
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if _parse_progress(text, encode_state):
                    _report_ffmpeg_progress(job, encode_state, duration, last_report)
        except Exception as exc:
            stdout_thread_error.append(exc)

    progress_thread = threading.Thread(target=read_encoder_progress, daemon=True)
    progress_thread.start()

    try:
        assert decoder.stdout is not None
        assert encoder.stdin is not None
        while True:
            if job.cancel_event.is_set():
                _terminate([decoder, encoder])
                break
            frame = _read_exact(decoder.stdout, frame_size)
            if not frame:
                break
            if len(frame) < frame_size:
                break
            try:
                processed_frame = engine.process_rgb(frame, source_width, source_height)
            except Exception as exc:
                raise RuntimeError(f"AI 推理失败：{exc}") from exc
            try:
                encoder.stdin.write(processed_frame)
                encoder.stdin.flush()
            except (BrokenPipeError, OSError):
                break
            processed += 1
            now = time.time()
            if now - last_report[0] > 0.4 or processed == total_frames:
                last_report[0] = now
                progress = min(99.0, processed / total_frames * 100)
                elapsed = max(0.001, now - (job.started_at or now))
                fps = processed / elapsed if elapsed > 0 else None
                remaining = max(0, total_frames - processed)
                eta = remaining / fps if fps and fps > 0 else None
                job.update(
                    progress=round(progress, 2),
                    fps=round(fps, 2) if fps else None,
                    speed=None,
                    eta_seconds=round(eta, 1) if eta else None,
                    processed_frames=processed,
                    total_frames=total_frames,
                )
    except Exception:
        _terminate([decoder, encoder])
        raise
    finally:
        try:
            if encoder.stdin and not encoder.stdin.closed:
                encoder.stdin.close()
        except Exception:
            pass

    try:
        decoder_return = decoder.wait(timeout=10)
    except subprocess.TimeoutExpired:
        _terminate([decoder])
        decoder_return = decoder.returncode
    try:
        encoder_return = encoder.wait(timeout=30)
    except subprocess.TimeoutExpired:
        _terminate([encoder])
        encoder_return = encoder.returncode
    progress_thread.join(timeout=2)
    _terminate([decoder, encoder])

    if job.cancel_event.is_set():
        if part.exists():
            part.unlink(missing_ok=True)
        raise RuntimeError("CANCELED")
    if decoder_return not in (0, None):
        if part.exists():
            part.unlink(missing_ok=True)
        raise RuntimeError(_friendly_error(decode_errors, decoder_return))
    if encoder_return != 0:
        if part.exists():
            part.unlink(missing_ok=True)
        raise RuntimeError(_friendly_error(encode_errors, encoder_return))
    if not part.exists() or part.stat().st_size == 0:
        raise RuntimeError("AI 处理没有生成有效输出，请尝试较小分辨率或更换模型")

    try:
        _validate_output(part, duration)
    except Exception:
        part.unlink(missing_ok=True)
        raise
    part.replace(output)
    job.update(progress=100.0)
    return output
