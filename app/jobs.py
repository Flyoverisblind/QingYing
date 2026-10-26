from __future__ import annotations

import os
import queue
import threading
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import config
from .ai import ai_available, ai_status
from .ffmpeg import binary_ok, probe_media
from .fs import resolve_output_path
from .schemas import JobOptions, resolve_target

_ALLOWED_UPDATE = {
    "status", "progress", "stage", "message", "error", "output", "speed", "fps",
    "eta_seconds", "processed_seconds", "processed_frames", "total_frames",
    "finished_at", "started_at", "canceling",
}


@dataclass
class Job:
    id: str
    source: str
    source_name: str
    options: JobOptions
    target: dict
    probe: dict
    output_path: Path
    preview: bool
    status: str = "queued"
    progress: float = 0.0
    stage: str = "排队中"
    message: str = ""
    error: str = ""
    output: str | None = None
    speed: float | None = None
    fps: float | None = None
    eta_seconds: float | None = None
    processed_seconds: float = 0.0
    processed_frames: int = 0
    total_frames: int = 0
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    canceling: bool = False
    logs: deque[str] = field(default_factory=lambda: deque(maxlen=300), repr=False)
    stderr_lines: deque[str] = field(default_factory=lambda: deque(maxlen=200), repr=False)
    processes: list[Any] = field(default_factory=list, repr=False)
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    on_change: Callable[["Job"], None] | None = field(default=None, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def update(self, **kwargs: Any) -> None:
        with self._lock:
            for key, value in kwargs.items():
                if key in _ALLOWED_UPDATE:
                    setattr(self, key, value)
            callback = self.on_change
        if callback:
            try:
                callback(self)
            except Exception:
                pass

    def add_log(self, line: str) -> None:
        if not line:
            return
        with self._lock:
            self.logs.append(str(line)[-2000:])

    def request_cancel(self) -> None:
        self.cancel_event.set()
        self.canceling = True
        self.update(stage="正在取消", message="正在停止编码器…")
        for process in list(self.processes):
            if process.poll() is None:
                try:
                    process.terminate()
                except Exception:
                    pass

    def snapshot(self, include_log: bool = False) -> dict[str, Any]:
        with self._lock:
            payload: dict[str, Any] = {
                "id": self.id,
                "source": self.source,
                "source_name": self.source_name,
                "status": self.status,
                "progress": round(self.progress, 2),
                "stage": self.stage,
                "message": self.message,
                "error": self.error,
                "output": self.output,
                "output_name": self.output_path.name if self.output else self.output_path.name,
                "preview": self.preview,
                "created_at": self.created_at,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "speed": self.speed,
                "fps": self.fps,
                "eta_seconds": self.eta_seconds,
                "processed_seconds": self.processed_seconds,
                "processed_frames": self.processed_frames,
                "total_frames": self.total_frames,
                "canceling": self.canceling,
                "target": self.target,
                "options": self.options.model_dump(),
                "probe": {
                    key: self.probe.get(key)
                    for key in ("width", "height", "fps", "duration", "video_codec", "audio_codec", "hdr")
                },
                "download_url": f"/api/jobs/{self.id}/file" if self.status == "done" else None,
                "output_size": (
                    Path(self.output).stat().st_size
                    if self.output and Path(self.output).exists()
                    else None
                ),
            }
            if include_log:
                payload["log"] = list(self.logs)[-80:]
            return payload


class JobManager:
    def __init__(self) -> None:
        self._jobs: "OrderedDict[str, Job]" = OrderedDict()
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._lock = threading.RLock()
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()
        self._current_id: str | None = None

    def start(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._stop.clear()
        self._worker = threading.Thread(target=self._run_worker, name="qingying-worker", daemon=True)
        self._worker.start()

    def shutdown(self) -> None:
        self._stop.set()
        with self._lock:
            running = self._jobs.get(self._current_id) if self._current_id else None
        if running:
            running.request_cancel()
        self._queue.put("__stop__")

    def create(self, source: str, options: JobOptions, preview: bool = False) -> Job:
        if not binary_ok(config.FFMPEG):
            raise RuntimeError("未找到 FFmpeg。请安装 FFmpeg，或把它放到程序目录的 bin 文件夹。")
        source_path = Path(source).expanduser().resolve()
        if not source_path.exists():
            raise FileNotFoundError(f"视频不存在：{source_path}")
        if source_path.suffix.lower() not in config.VIDEO_EXTENSIONS:
            raise ValueError("不支持的文件格式")
        if not source_path.is_file():
            raise ValueError("请选择视频文件")

        probe = probe_media(source_path)
        if float(probe.get("duration") or 0) <= 0:
            raise ValueError("无法读取视频时长")

        options = options.model_copy(deep=True)
        if preview:
            options.container = "mp4"
            options.audio = "copy" if probe.get("has_audio") else "none"
            max_end = min(float(probe["duration"]), options.start + 10.0)
            if options.end is None or options.end > max_end:
                options.end = max_end

        if options.sr_engine == "ai":
            status = ai_status()
            if not status.get("available"):
                reason = status.get("reason") or "未检测到兼容的 Vulkan 显卡"
                raise ValueError(f"AI 超分当前不可用：{reason}")

        target = resolve_target(probe, options)
        if options.output_dir:
            output_dir = Path(options.output_dir).expanduser()
            if output_dir.exists() and not output_dir.is_dir():
                raise ValueError("输出路径不是文件夹")
            try:
                output_dir.mkdir(parents=True, exist_ok=True)
                test_file = output_dir / f".qingying_write_{uuid.uuid4().hex[:6]}"
                test_file.write_bytes(b"ok")
                test_file.unlink()
            except OSError as exc:
                raise ValueError(f"输出目录不可写：{exc}") from exc

        if options.encoder:
            from .ffmpeg import resolve_encoder

            resolve_encoder(options.codec, options.encoder)

        output_path = resolve_output_path(
            source_path, options, target, job_id=uuid.uuid4().hex[:8], preview=preview
        )
        job = Job(
            id=uuid.uuid4().hex[:10],
            source=str(source_path),
            source_name=source_path.name,
            options=options,
            target=target,
            probe=probe,
            output_path=output_path,
            preview=preview,
        )
        if preview:
            output_path = output_path.with_name(f"preview_{job.id}.mp4")
            job.output_path = output_path

        with self._lock:
            self._jobs[job.id] = job
            while len(self._jobs) > config.MAX_JOBS_KEPT:
                oldest_id, oldest = next(iter(self._jobs.items()))
                if oldest.status in {"running", "queued"}:
                    break
                self._jobs.pop(oldest_id, None)
            self._queue.put(job.id)
        job.add_log(f"任务已创建：{source_path.name}")
        job.add_log(
            f"输出：{output_path} · {target['width']}x{target['height']} · "
            f"{target['fps']:.2f} fps"
        )
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            jobs = list(self._jobs.values())
        jobs.sort(key=lambda item: item.created_at, reverse=True)
        return [job.snapshot() for job in jobs]

    def active_count(self) -> int:
        with self._lock:
            return sum(1 for job in self._jobs.values() if job.status in {"running", "queued"})

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if not job:
            return False
        if job.status == "queued":
            job.cancel_event.set()
            job.update(
                status="canceled",
                stage="已取消",
                message="任务在开始前被取消",
                finished_at=time.time(),
            )
            return True
        if job.status == "running":
            job.request_cancel()
            return True
        return False

    def remove(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.status in {"running", "queued"}:
                return False
            self._jobs.pop(job_id, None)
            return True

    def _run_worker(self) -> None:
        from .runner import run_ai_job, run_filter_job

        while not self._stop.is_set():
            job_id = self._queue.get()
            try:
                if job_id == "__stop__":
                    break
                job = self.get(job_id)
                if not job:
                    continue
                if job.status != "queued":
                    continue
                if job.cancel_event.is_set():
                    job.update(status="canceled", stage="已取消", finished_at=time.time())
                    continue

                self._current_id = job.id
                job.update(
                    status="running",
                    started_at=time.time(),
                    stage="分析视频",
                    message="正在读取视频信息…",
                    progress=0.0,
                )
                try:
                    if job.options.sr_engine == "ai":
                        output = run_ai_job(job, job.probe, job.target)
                    else:
                        output = run_filter_job(job, job.probe, job.target)
                    job.update(
                        status="done",
                        progress=100.0,
                        stage="已完成",
                        message="处理完成",
                        output=str(output),
                        finished_at=time.time(),
                        eta_seconds=0,
                    )
                    job.add_log(f"完成：{output}")
                except Exception as exc:
                    if job.cancel_event.is_set() or str(exc) == "CANCELED":
                        job.update(
                            status="canceled",
                            stage="已取消",
                            message="任务已取消",
                            finished_at=time.time(),
                            eta_seconds=None,
                        )
                        job.add_log("任务已取消")
                    else:
                        message = str(exc) or "未知错误"
                        job.update(
                            status="error",
                            stage="处理失败",
                            error=message,
                            message=message,
                            finished_at=time.time(),
                            eta_seconds=None,
                        )
                        job.add_log(f"错误：{message}")
                finally:
                    job.processes.clear()
                    self._current_id = None
            finally:
                self._queue.task_done()


manager = JobManager()


def warm_ai_probe() -> None:
    try:
        ai_status()
    except Exception:
        pass


def system_snapshot() -> dict[str, Any]:
    from .ffmpeg import check_binaries, codec_options, detect_encoders
    from .fs import disk_space

    binaries = check_binaries()
    encoders = detect_encoders()
    return {
        "ok": binaries["ffmpeg_ok"],
        "binaries": binaries,
        "encoders": codec_options(),
        "hardware": encoders.get("hardware", {}),
        "ai": ai_status(),
        "platform": config.SYSTEM_INFO,
        "cpu_count": os.cpu_count() or 1,
        "disk": disk_space(config.OUTPUT_DIR),
        "output_dir": str(config.OUTPUT_DIR),
        "data_dir": str(config.DATA_DIR),
        "version": config.VERSION,
    }
